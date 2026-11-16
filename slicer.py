"""Slicer çıktısından (G-code / dilimlenmiş .3mf) gram, süre ve renk bilgisini okur.

Anycubic Slicer Next (Orca/Bambu tabanlı) G-code'un sonuna şu satırları yazar:
  ; filament used [g] = 70.89, 0.00, 0.00, 31.74
  ; estimated printing time (normal mode) = 6h 34m 32s
  ; filament_colour = #EFF0F1;#212721;#E10600;#003594
  ; filament_type = PLA;PLA;PLA;PLA
Dilimlenmiş .3mf içinde Metadata/slice_info.config (gram/renk) ve Metadata/plate_N.gcode bulunur.
"""
from __future__ import annotations

import glob
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

TAIL = 1_500_000  # özet + (Anycubic profillerinde) ayar bloğu dosyanın sonunda
HEAD = 400_000    # Bambu tabanlı projelerde ayar bloğu dosyanın başında
SLICER_BINS = ["/Applications/AnycubicSlicerNext.app/Contents/MacOS/AnycubicSlicerNext",
               os.path.expanduser("~/Applications/AnycubicSlicerNext.app/Contents/MacOS/AnycubicSlicerNext")]


def _dur(s: str) -> float | None:
    """'1d 6h 34m 32s' → saat."""
    parts = dict((u, float(v)) for v, u in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", s or ""))
    if not parts:
        return None
    return round(parts.get("d", 0) * 24 + parts.get("h", 0) + parts.get("m", 0) / 60 + parts.get("s", 0) / 3600, 2)


def _kv(text: str) -> dict:
    out = {}
    for m in re.finditer(r"^;\s*([^=:\n]+?)\s*[=:]\s*(.*)$", text, re.M):
        out.setdefault(m.group(1).strip().lower(), m.group(2).strip())
    return out


def _floats(s: str) -> list[float]:
    try:
        return [float(x) for x in re.split(r"[,;]\s*", s.strip()) if x.strip()]
    except ValueError:
        return []


def clean_name(fname: str) -> str:
    """'1114-1647-100_black_plate(01)_PLA_0.2_6h34m32s.gcode' → '100_black'."""
    n = os.path.basename(fname or "")
    n = re.sub(r"\.(gcode|3mf|bgcode)$", "", n, flags=re.I)
    n = re.sub(r"\.gcode$", "", n, flags=re.I)
    n = re.sub(r"^\d{4}-\d{4}-", "", n)                     # zaman damgası
    n = re.sub(r"_[A-Z+\-]+_\d+(\.\d+)?_[\dhms]+$", "", n)    # _PLA_0.2_6h34m32s
    n = re.sub(r"_?plate\(?\d*\)?$", "", n, flags=re.I)
    return n.replace("_", " ").strip() or os.path.basename(fname or "")


def parse_gcode_text(text: str, fname: str = "") -> dict:
    kv = _kv(text)
    grams = _floats(kv.get("filament used [g]", ""))
    cols = [c.strip() for c in kv.get("filament_colour", "").split(";")]
    types = [t.strip() for t in kv.get("filament_type", "").split(";")]
    hours = _dur(kv.get("estimated printing time (normal mode)", "")) or _dur(kv.get("print_time", ""))
    if not hours:
        m = re.search(r"total estimated time:\s*([\d dhms]+)", text)
        hours = _dur(m.group(1)) if m else None
    if not grams and kv.get("total filament used [g]"):
        grams = _floats(kv["total filament used [g]"])
    if not grams:
        raise ValueError("Dosyada gram bilgisi yok. Dilimlenmiş G-code (ya da 'G-code 3mf') dosyası olmalı.")
    items = [{"slot": i, "g": round(gr, 2), "color": (cols[i] if i < len(cols) else "") or None,
              "type": (types[i] if i < len(types) else "") or "PLA"} for i, gr in enumerate(grams) if gr > 0.004]
    return {"name": clean_name(fname), "file": os.path.basename(fname or ""), "hours": hours,
            "total_g": round(sum(i["g"] for i in items), 2), "items": items,
            "printer": kv.get("printer_model") or None}


def head_tail(raw: bytes) -> str:
    if len(raw) <= HEAD + TAIL:
        return raw.decode("utf-8", "replace")
    return (raw[:HEAD] + b"\n" + raw[-TAIL:]).decode("utf-8", "replace")


def parse_gcode_file(path: str, name: str | None = None) -> dict:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size <= HEAD + TAIL:
            f.seek(0)
            raw = f.read()
        else:
            f.seek(0)
            raw = f.read(HEAD) + b"\n"
            f.seek(size - TAIL)
            raw += f.read()
    return parse_gcode_text(raw.decode("utf-8", "replace"), name or path)


def combine(plates: list[dict], name: str) -> dict:
    """Tüm tablaların toplamı; renkleri slot bazında birleştirir."""
    by = {}
    for p in plates:
        for it in p["items"]:
            k = (it["slot"], it["color"], it["type"])
            by[k] = round(by.get(k, 0) + it["g"], 2)
    items = [{"slot": k[0], "color": k[1], "type": k[2], "g": v} for k, v in by.items()]
    hrs = [p["hours"] for p in plates if p.get("hours")]
    return {"name": name, "file": plates[0]["file"], "hours": round(sum(hrs), 2) if hrs else None,
            "total_g": round(sum(i["g"] for i in items), 2), "items": items, "printer": plates[0].get("printer")}


def slice_3mf(data: bytes, fname: str) -> dict:
    """Dilimlenmemiş projeyi Anycubic Slicer Next'in komut satırıyla arka planda dilimler."""
    exe = next((b for b in SLICER_BINS if os.path.exists(b)), None)
    if not exe:
        raise ValueError("Bu .3mf dilimlenmemiş ve Anycubic Slicer Next bulunamadı. Slicer'da dilimleyip "
                         "'Slicer'daki son dilimleme' butonunu kullan.")
    tmp = tempfile.mkdtemp(prefix="ft-slice-")
    try:
        src = os.path.join(tmp, "model.3mf")
        with open(src, "wb") as f:
            f.write(data)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        try:
            p = subprocess.run([exe, "--debug", "1", "--slice", "0", "--outputdir", out, src],
                               capture_output=True, timeout=240, cwd=tmp)
        except subprocess.TimeoutExpired as e:
            raise ValueError("Dilimleme 4 dakikada bitmedi; model çok büyük olabilir") from e
        files = sorted(glob.glob(os.path.join(out, "plate_*.gcode")), key=lambda x: int(re.findall(r"\d+", os.path.basename(x))[0]))
        if not files:
            err = [ln for ln in p.stderr.decode("utf-8", "replace").splitlines() + p.stdout.decode("utf-8", "replace").splitlines()
                   if "[error]" in ln and "exclude_triangles" not in ln]
            raise ValueError("Slicer bu dosyayı dilimleyemedi" + (f": {err[-1].split(']')[-1].strip()}" if err else ""))
        name = clean_name(fname)
        plates = []
        for i, gp in enumerate(files, 1):
            r = parse_gcode_file(gp, fname)
            r["plate"] = int(re.findall(r"\d+", os.path.basename(gp))[0])
            r["sliced"] = True
            r["name"] = name if len(files) == 1 else f"{name} · tabla {r['plate']}"
            plates.append(r)
        res = combine(plates, name) if len(plates) > 1 else dict(plates[0])
        res["sliced"] = True
        if len(plates) > 1:
            res["plates"] = plates
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def parse_3mf(data: bytes, fname: str = "") -> dict:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("Geçerli bir .3mf dosyası değil") from e
    names = z.namelist()
    # 1) içinde dilimlenmiş gcode varsa onu oku (en güvenilir)
    gcodes = sorted(n for n in names if re.match(r"Metadata/plate_\d+\.gcode$", n))
    if gcodes:
        plates = []
        for gname in gcodes:
            r = parse_gcode_text(head_tail(z.read(gname)), fname)
            r["plate"] = int(re.findall(r"\d+", gname)[0])
            r["name"] = clean_name(fname) if len(gcodes) == 1 else f"{clean_name(fname)} · tabla {r['plate']}"
            plates.append(r)
        if len(plates) == 1:
            return plates[0]
        res = combine(plates, clean_name(fname))
        res["plates"] = plates
        return res
    # 2) slice_info.config içinde filament kullanımı
    if "Metadata/slice_info.config" in names:
        root = ET.fromstring(z.read("Metadata/slice_info.config"))
        plate = root.find("plate")
        if plate is not None:
            meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
            items = []
            for fl in plate.findall("filament"):
                gr = float(fl.get("used_g") or 0)
                if gr > 0:
                    items.append({"slot": int(fl.get("id") or 1) - 1, "g": round(gr, 2),
                                  "color": fl.get("color"), "type": fl.get("type") or "PLA"})
            if items:
                pred = float(meta.get("prediction") or 0)
                return {"name": clean_name(fname), "file": os.path.basename(fname), "hours": round(pred / 3600, 2) if pred else None,
                        "total_g": round(sum(i["g"] for i in items), 2), "items": items, "printer": None}
    return slice_3mf(data, fname)


def recent_slices(limit: int = 6) -> list[dict]:
    """Anycubic Slicer Next'in geçici klasöründeki son dilimlemeler (yeniden eskiye)."""
    roots = {tempfile.gettempdir(), os.environ.get("TMPDIR", "")}
    files = []
    for r in filter(None, roots):
        files += glob.glob(os.path.join(r, "anycubicslicer_model", "*", "*", "Metadata", "*.gcode"))
    seen, out = set(), []
    for p in sorted(set(files), key=lambda p: os.path.getmtime(p), reverse=True):
        b = os.path.basename(p)
        if b.startswith("."):
            continue
        key = (b, os.path.getsize(p))
        if key in seen:
            continue
        seen.add(key)
        try:
            r = parse_gcode_file(p)
        except (OSError, ValueError):
            continue
        r["mtime"] = os.path.getmtime(p)
        out.append(r)
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    t = time.time()
    for r in recent_slices():
        print(r)
    print(f"{time.time() - t:.2f}s")
