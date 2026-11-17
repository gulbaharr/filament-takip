#!/usr/bin/env python3
"""Filament Takip — yerel filament stok takibi + Anycubic Cloud entegrasyonu.

Sadece Python standart kütüphanesi kullanır. Çalıştır:  python3 server.py
Arayüz: http://127.0.0.1:8765
Veri:   ~/Library/Application Support/FilamentTakip/data.json
"""
from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lan as lanmod  # noqa: E402
import slicer as slicermod  # noqa: E402

HOST, PORT = "127.0.0.1", int(os.environ.get("FILAMENT_PORT", "8765"))
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / "Library" / "Application Support" / "FilamentTakip"
DATA_FILE = DATA_DIR / "data.json"
POLL_IDLE, POLL_PRINTING = 60, 20  # saniye

# --- Anycubic Cloud (web istemcisinin imza parametreleri) -------------------
API = "https://cloud-universe.anycubic.com/p/p/workbench/api"
APP_ID = "f9b3528877c94d5c9c5af32245db46ef"
APP_SECRET = "0cf75926606049a3937f56b0373b99fb"
APP_VERSION = "1.0.0"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

DENSITY = {"PLA": 1.24, "PLA+": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07,
           "TPU": 1.21, "PA": 1.14, "PC": 1.20, "HIPS": 1.04, "PVA": 1.23}
FIL_AREA_MM2 = math.pi * 0.875 ** 2  # 1.75 mm filament

lock = threading.RLock()
wake = threading.Event()


# --- Veri katmanı -----------------------------------------------------------
def default_state() -> dict:
    return {"token": "", "printer_id": None, "spools": [], "history": [],
            "slot_map": {}, "track": None, "live": None, "log": [],
            "orders": [], "purchases": [], "products": [],
            "settings": {"low_g": 150, "auto_deduct": True, "mode": "manual", "lan_host": "",
                         "kwh_price": 3.0, "printer_watt": 150, "labor_hour": 0, "margin": 30, "default_kg": 0, "order_deduct": True,
                         "currency": "₺"}}


def load() -> dict:
    try:
        s = json.loads(DATA_FILE.read_text("utf-8"))
        base = default_state()
        base.update(s)
        base["settings"] = {**default_state()["settings"], **s.get("settings", {})}
        if "mode" not in s.get("settings", {}):
            base["settings"]["mode"] = "cloud" if s.get("token") else "manual"
        return base
    except FileNotFoundError:
        return default_state()
    except Exception as e:  # bozuk dosyayı yedekle, sıfırdan başla
        DATA_FILE.rename(DATA_FILE.with_suffix(f".bozuk-{int(time.time())}.json"))
        print("data.json okunamadı, yedeklendi:", e)
        return default_state()


def save() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(DATA_FILE)


def log(msg: str) -> None:
    with lock:
        STATE["log"].insert(0, {"ts": time.time(), "msg": msg})
        del STATE["log"][80:]
    print(time.strftime("%H:%M:%S"), msg)


STATE = load()


def spool_by_id(sid):
    return next((s for s in STATE["spools"] if s["id"] == sid), None)


def _own_gp(sp) -> float:
    return float(sp.get("price") or 0) / sp["total_g"] if sp and sp.get("price") and sp.get("total_g") else 0.0


def _avg_gp(spools) -> float:
    ps = [s for s in spools if _own_gp(s)]
    tg = sum(s["total_g"] for s in ps)
    return sum(float(s["price"]) for s in ps) / tg if tg else 0.0


def spool_gram_price(sp) -> float:
    """Makaranın ₺/g fiyatı; yoksa aynı renk → aynı malzeme → genel ortalama → varsayılan."""
    if _own_gp(sp):
        return _own_gp(sp)
    allsp = STATE["spools"]
    if sp:
        mat = lambda s: str(s.get("material") or "PLA").upper().strip()
        col = lambda s: str(s.get("name") or s.get("color") or "").strip().lower()
        for same in ([s for s in allsp if mat(s) == mat(sp) and col(s) == col(sp)], [s for s in allsp if mat(s) == mat(sp)]):
            p = _avg_gp(same)
            if p:
                return p
    return _avg_gp(allsp) or float(STATE["settings"].get("default_kg") or 0) / 1000


def finalize_entry(e: dict) -> dict:
    """Geçmiş kaydına filament maliyeti ve süre ekle (o anki fiyatlarla sabitlenir)."""
    e["fil_cost"] = round(sum(gr * spool_gram_price(spool_by_id(k)) for k, gr in (e.get("per_spool") or {}).items()), 2)
    if e.get("hours") is None and e.get("start") and e.get("ts") and e["ts"] > e["start"]:
        e["hours"] = round((e["ts"] - e["start"]) / 3600, 2)
    return e


# --- Sipariş "Basılıyor" → tahmini gramı stoktan düş (gerçek baskıyla mahsuplaşır) ----
def trlower(x) -> str:
    return str(x or "").strip().replace("İ", "i").replace("I", "ı").lower()


def mat_of(sp) -> str:
    return str(sp.get("material") or "PLA").upper().strip()


def grp_key(sp) -> str:  # index.html'deki grpKey ile aynı
    return mat_of(sp) + "|" + trlower(sp.get("name") or sp.get("color") or "")


def reserve_entry(oid):
    return next((h for h in STATE["history"] if h.get("order_id") == oid and h.get("kind") == "reserve"), None)


def _spool_order(cands):
    loaded = set(STATE["slot_map"].values())
    return sorted(cands, key=lambda s: (s["id"] not in loaded, s.get("remaining_g") or 0, s.get("created") or 0))


def reserve_order(o: dict) -> str | None:
    if reserve_entry(o["id"]):
        return None
    q = float(o.get("qty") or 1)
    items = [i for i in (o.get("est_items") or []) if float(i.get("g") or 0) > 0]
    if not items and float(o.get("est_g") or 0) > 0:
        items = [{"ref": "", "g": o["est_g"]}]
    if not items:
        return "Siparişte filament tahmini yok; stoktan bir şey düşülmedi."
    printed = {}  # bu siparişe zaten basılmış gerçek gramlar (grup bazında)
    t = STATE.get("track")
    for h in STATE["history"] + ([t] if t and t.get("order_id") == o["id"] else []):
        if h.get("order_id") == o["id"] and h.get("kind") != "reserve":
            for sid, gr in (h.get("per_spool") or {}).items():
                sp = spool_by_id(sid)
                k = grp_key(sp) if sp else "?"
                printed[k] = printed.get(k, 0) + gr
    per, short, used_any = {}, 0.0, False
    for it in items:
        ref, need = str(it.get("ref") or ""), float(it["g"]) * q
        if ref.startswith("g:"):
            keys = [ref[2:]]
            cands = [s for s in STATE["spools"] if grp_key(s) == ref[2:]]
        elif ref.startswith("m:"):
            keys = [k for k in printed if k.split("|")[0] == ref[2:]]
            cands = [s for s in STATE["spools"] if mat_of(s) == ref[2:]]
        else:
            keys, cands = list(printed), [s for s in STATE["spools"] if s["id"] in STATE["slot_map"].values()] or STATE["spools"]
        for k in keys:
            t = min(need, printed.get(k, 0))
            printed[k] = printed.get(k, 0) - t
            need -= t
        for sp in _spool_order([s for s in cands if (s.get("remaining_g") or 0) > 0]):
            if need <= 0.05:
                break
            t = min(need, sp["remaining_g"])
            sp["remaining_g"] -= t
            per[sp["id"]] = per.get(sp["id"], 0) + t
            need -= t
            used_any = True
        short += max(0.0, need)
    if not used_any:
        return "Bu renklerde stok yok; stoktan bir şey düşülmedi." if short else None
    now = time.time()
    grams = round(sum(per.values()), 1)
    hrs = float(o.get("est_h") or 0) * q or None
    STATE["history"].insert(0, finalize_entry({
        "ts": now, "start": now, "file": f"{o.get('item') or 'Sipariş'} (tahmini)", "task_id": None, "kind": "reserve",
        "grams": grams, "orig_grams": grams, "per_spool": {k: round(v, 1) for k, v in per.items()},
        "progress": None, "hours": hrs, "est_hours": hrs, "order_id": o["id"], "short": round(short, 1)}))
    names = ", ".join(f"{(spool_by_id(k) or {}).get('name') or 'makara'} {v:.0f} g" for k, v in per.items())
    log(f"Sipariş basılıyor: {o.get('item') or '—'} · stoktan düşüldü: {names}")
    return f"Stoktan düşüldü: {names}" + (f" · {short:.0f} g stok yetmedi" if short > 0.5 else "")


def unreserve_order(oid) -> str | None:
    r = reserve_entry(oid)
    if not r:
        return None
    for sid, gr in (r.get("per_spool") or {}).items():
        sp = spool_by_id(sid)
        if sp:
            sp["remaining_g"] = min(sp["total_g"], sp["remaining_g"] + gr)
    STATE["history"] = [h for h in STATE["history"] if h is not r]
    log(f"Tahmini düşüm geri alındı: {r.get('grams')} g stoğa iade")
    return f"{r.get('grams', 0):.0f} g stoğa geri eklendi"


def reconcile(oid, used: dict) -> None:
    """Siparişe bağlı gerçek tüketim kadar tahmini düşümü geri ver (çift düşmeyi önler)."""
    r = reserve_entry(oid) if oid else None
    if not r:
        return
    rp = r["per_spool"]
    for sid, g in used.items():
        real = spool_by_id(sid)
        order = [sid] if sid in rp else []
        if real:
            order += [k for k in rp if k not in order and spool_by_id(k) and grp_key(spool_by_id(k)) == grp_key(real)]
            order += [k for k in rp if k not in order and spool_by_id(k) and mat_of(spool_by_id(k)) == mat_of(real)]
        order += [k for k in rp if k not in order]
        for k in order:
            if g <= 0.01:
                break
            t = min(g, rp.get(k, 0))
            if t <= 0:
                continue
            rp[k] = round(rp[k] - t, 2)
            g -= t
            sp = spool_by_id(k)
            if sp:
                sp["remaining_g"] = min(sp["total_g"], sp["remaining_g"] + t)
    r["per_spool"] = {k: v for k, v in rp.items() if v > 0.05}
    r["grams"] = round(sum(r["per_spool"].values()), 1)
    if r["grams"] < 0.5:
        STATE["history"] = [h for h in STATE["history"] if h is not r]
        return
    if r.get("est_hours") and r.get("orig_grams"):
        r["hours"] = round(r["est_hours"] * r["grams"] / r["orig_grams"], 2)
    finalize_entry(r)


def grams_from_mm(mm: float, material: str | None) -> float:
    d = DENSITY.get((material or "PLA").upper().strip(), 1.24)
    return mm * FIL_AREA_MM2 / 1000.0 * d


# --- Anycubic Cloud istemcisi -----------------------------------------------
class CloudError(Exception):
    pass


def cloud_get(path: str, params: dict | None = None) -> dict:
    token = STATE.get("token")
    if not token:
        raise CloudError("Token yok")
    nonce = str(uuid.uuid1())
    ts = str(int(time.time() * 1000))
    sig = hashlib.md5(f"{APP_ID}{ts}{APP_VERSION}{APP_SECRET}{nonce}{APP_ID}".encode()).hexdigest()
    headers = {
        "User-Agent": UA, "Origin": "https://cloud-universe.anycubic.com",
        "Xx-Device-Type": "web", "Xx-Is-Cn": "1", "Xx-Nonce": nonce,
        "Xx-Signature": sig, "Xx-Timestamp": ts, "Xx-Version": APP_VERSION,
        "Content-Type": "application/json", "XX-Token": token, "XX-LANGUAGE": "US",
    }
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise CloudError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise CloudError(f"Bağlantı hatası: {e.reason}") from e
    if body.get("code") != 1:
        raise CloudError(f"Anycubic: {body.get('msg') or body.get('code')}")
    return body.get("data")


def norm_color(c) -> str | None:
    if isinstance(c, list) and len(c) >= 3:
        return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in c[:3])
    if isinstance(c, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", c):
        return "#" + c.lstrip("#").lower()
    return None


def parse_info(info: dict) -> dict:
    boxes = info.get("multi_color_box") or []
    if isinstance(boxes, dict):
        boxes = [boxes]
    slots, loaded = [], None
    for bi, box in enumerate(boxes):
        ls = box.get("loaded_slot", -1)
        for s in box.get("slots") or []:
            key = f"{bi}:{s.get('index')}"
            empty = s.get("color") == -2 or not s.get("type")
            slots.append({"key": key, "box": bi, "index": s.get("index"),
                          "material": s.get("type") or "", "color": norm_color(s.get("color")),
                          "sku": s.get("sku") or "", "status": s.get("status"), "empty": bool(empty)})
            if ls is not None and ls >= 0 and s.get("index") == ls:
                loaded = key
    proj = info.get("project") or {}
    status_raw = info.get("is_printing")
    printing = bool(proj) and (status_raw == 2 or proj.get("print_status") == 1)
    est = proj.get("estimate_supplies_usage")  # metre
    used = proj.get("supplies_usage")
    return {
        "name": info.get("name"), "model": info.get("machine_type") or info.get("model"),
        "online": info.get("device_status", info.get("online")),
        "printing": printing, "is_printing": status_raw,
        "task_id": proj.get("task_id"), "file": proj.get("gcode_name") or proj.get("name"),
        "progress": proj.get("progress"), "remain_min": proj.get("remain_time"),
        "est_m": float(est) if isinstance(est, (int, float, str)) and str(est).replace(".", "", 1).isdigit() else None,
        "used_mm": float(used) if isinstance(used, (int, float)) else None,
        "slots": slots, "loaded": loaded, "updated": time.time(),
    }


def _deduct(t: dict, live: dict, new_progress) -> None:
    """t['progress'] → new_progress arası tüketimi takılı slottaki makaradan düş."""
    if new_progress is None:
        return
    total_mm = (live.get("est_m") or t.get("est_m") or 0) * 1000.0 or (live.get("used_mm") or 0)
    dp = max(0, new_progress - (t.get("progress") or 0))
    t["progress"] = max(new_progress, t.get("progress") or 0)
    if live.get("est_m"):
        t["est_m"] = live["est_m"]
    if not (total_mm and dp):
        return
    slot = live.get("loaded")
    sid = STATE["slot_map"].get(slot) if slot else None
    sp = spool_by_id(sid) if sid else None
    mat = sp["material"] if sp else next((s["material"] for s in live.get("slots", []) if s["key"] == slot), "PLA")
    g = grams_from_mm(dp / 100.0 * total_mm, mat)
    t["grams"] += g
    k = sid or f"slot:{slot}"
    t["per_spool"][k] = t["per_spool"].get(k, 0) + g
    if sp and STATE["settings"]["auto_deduct"]:
        sp["remaining_g"] = max(0.0, sp["remaining_g"] - g)
        if t.get("order_id"):
            reconcile(t["order_id"], {sp["id"]: g})


def track(live: dict) -> None:
    """Baskı ilerledikçe takılı slottaki makaradan gram düş."""
    t = STATE.get("track")
    if live["printing"] and live["task_id"]:
        if t and t.get("task_id") != live["task_id"]:
            _finish(t)
            t = None
        if not t:
            # Baskının ortasında fark edilse bile 0'dan say: harcanan filament gerçekten harcandı.
            t = STATE["track"] = {"task_id": live["task_id"], "file": live["file"], "start": time.time(),
                                  "progress": 0, "est_m": live.get("est_m"), "grams": 0.0,
                                  "per_spool": {}, "order_id": None}
            log(f"Baskı algılandı: {live['file']}")
        _deduct(t, live, live["progress"])
    elif t:
        # Yazıcı "bitti" diyorsa kalan kısmı da düş; iptalde olduğu yerde kalır.
        if live.get("state") == "finished" or (live.get("progress") or 0) >= 100:
            _deduct(t, live, 100)
        _finish(t)


def _finish(t: dict) -> None:
    STATE["history"].insert(0, finalize_entry({
        "ts": time.time(), "start": t["start"], "file": t.get("file"), "task_id": t["task_id"],
        "grams": round(t["grams"], 1), "per_spool": {k: round(v, 1) for k, v in t["per_spool"].items()},
        "progress": t.get("progress"), "order_id": t.get("order_id"), "reconciled": t.get("order_id")}))
    del STATE["history"][300:]
    log(f"Baskı bitti: {t.get('file')} · {t['grams']:.1f} g")
    STATE["track"] = None


def poll_once() -> None:
    printers = cloud_get("/work/printer/getPrinters") or []
    if not printers:
        raise CloudError("Hesapta yazıcı bulunamadı")
    with lock:
        ids = [p.get("id") for p in printers]
        if STATE["printer_id"] not in ids:
            STATE["printer_id"] = ids[0]
        STATE["printers"] = [{"id": p.get("id"), "name": p.get("name")} for p in printers]
        pid = STATE["printer_id"]
    info = cloud_get("/v2/printer/info", {"id": pid}) or {}
    live = parse_info(info)
    with lock:
        STATE["live"] = live
        STATE["raw"] = info
        STATE["cloud_error"] = None
        track(live)
        save()


LAN = None


def ensure_lan():
    """Ayarlara göre LAN istemcisini başlat/durdur."""
    global LAN
    st = STATE["settings"]
    want = st.get("lan_host") if st.get("mode") == "lan" else None
    if LAN and (not want or LAN.host != want):
        LAN.stop()
        LAN = None
    if want and not LAN:
        LAN = lanmod.LanClient(want, on_update=wake.set, log=log)
        LAN.start()


def poller() -> None:
    while True:
        interval = POLL_IDLE
        ensure_lan()
        mode = STATE["settings"].get("mode")
        if mode == "lan":
            interval = 10
            live = LAN.live() if LAN else None
            with lock:
                if live:
                    STATE["live"], STATE["raw"] = live, LAN.raw
                    track(live)
                    save()
        elif mode == "cloud" and STATE.get("token"):
            try:
                poll_once()
                if STATE.get("track"):
                    interval = POLL_PRINTING
            except CloudError as e:
                with lock:
                    if STATE.get("cloud_error") != str(e):
                        log(f"Bulut hatası: {e}")
                    STATE["cloud_error"] = str(e)
            except Exception as e:  # beklenmeyen veri formatı vb.
                with lock:
                    STATE["cloud_error"] = f"Beklenmeyen hata: {e!r}"
                log(STATE["cloud_error"])
        wake.wait(interval)
        wake.clear()


# --- Tarayıcıdan token bulma (Chrome/Brave/Edge/Arc localStorage) -----------
def detect_token() -> str | None:
    roots = ["Google/Chrome", "BraveSoftware/Brave-Browser", "Microsoft Edge", "Arc/User Data",
             "Vivaldi", "Chromium"]
    sup = Path.home() / "Library" / "Application Support"
    best = None
    for r in roots:
        for f in glob.glob(str(sup / r / "*" / "Local Storage" / "leveldb" / "*")):
            if not f.endswith((".ldb", ".log")):
                continue
            try:
                data = Path(f).read_bytes()
            except OSError:
                continue
            if b"cloud-universe.anycubic.com" not in data:
                continue
            for m in re.finditer(rb"XX-Token.{1,8}?(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)", data, re.S):
                tok = m.group(1).decode()
                mt = os.path.getmtime(f)
                if not best or mt > best[0]:
                    best = (mt, tok)
    return best[1] if best else None


# --- HTTP sunucu ------------------------------------------------------------
def public_state() -> dict:
    with lock:
        s = {k: v for k, v in STATE.items() if k not in ("token", "raw")}
        s["has_token"] = bool(STATE.get("token"))
        s["lan_status"] = {"connected": bool(LAN and LAN.connected), "error": LAN.error if LAN else None}
        s["token_hint"] = (STATE["token"][:6] + "…" + STATE["token"][-4:]) if STATE.get("token") else ""
        return json.loads(json.dumps(s))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code=200, obj=None, ctype="application/json", raw=None):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self.send(raw=(APP_DIR / "index.html").read_bytes(), ctype="text/html")
        if self.path == "/api/state":
            return self.send(obj=public_state())
        if self.path == "/api/slicer/recent":
            try:
                return self.send(obj={"items": slicermod.recent_slices()})
            except Exception as e:  # noqa: BLE001
                return self.send(400, {"error": f"Slicer klasörü okunamadı: {e}"})
        if self.path == "/api/raw":
            with lock:
                return self.send(obj=STATE.get("raw"))
        self.send(404, {"error": "yok"})

    def do_POST(self):
        # Yalnızca kendi arayüzümüzden gelen istekler (CSRF koruması)
        if self.headers.get("X-Filament") != "1":
            return self.send(403, {"error": "yasak"})
        if self.path.startswith("/api/slicer/parse"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name, n = (q.get("name") or ["dosya"])[0], int(self.headers.get("Content-Length") or 0)
            if not n or n > 400_000_000:
                return self.send(400, {"error": "Dosya boş ya da çok büyük"})
            data = self.rfile.read(n)
            try:
                if name.lower().endswith(".3mf"):
                    r = slicermod.parse_3mf(data, name)
                else:
                    r = slicermod.parse_gcode_text(data.decode("utf-8", "replace"), name)
                return self.send(obj=r)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        try:
            d = self.body()
            r = self.route(self.path, d)
            self.send(obj=r if r is not None else public_state())
        except ValueError as e:
            self.send(400, {"error": str(e)})

    def route(self, path, d):
        with lock:
            if path == "/api/spool":
                sp = spool_by_id(d.get("id")) if d.get("id") else None
                if not sp:
                    sp = {"id": uuid.uuid4().hex[:10], "created": time.time()}
                    STATE["spools"].append(sp)
                for k in ("name", "brand", "material", "color", "notes"):
                    if k in d:
                        sp[k] = str(d[k] or "").strip()
                for k in ("total_g", "remaining_g", "price", "tare_g"):
                    if k in d and d[k] not in ("", None):
                        sp[k] = float(d[k])
                sp.setdefault("total_g", 1000.0)
                sp.setdefault("remaining_g", sp["total_g"])
                sp.setdefault("material", "PLA")
                save()
                return {"ok": True, "id": sp["id"]}
            if path == "/api/spool/delete":
                STATE["spools"] = [s for s in STATE["spools"] if s["id"] != d.get("id")]
                STATE["slot_map"] = {k: v for k, v in STATE["slot_map"].items() if v != d.get("id")}
                save()
                return None
            if path == "/api/spool/adjust":
                sp = spool_by_id(d.get("id"))
                if not sp:
                    raise ValueError("makara yok")
                sp["remaining_g"] = max(0.0, sp["remaining_g"] + float(d.get("delta", 0)))
                save()
                return None
            if path == "/api/assign":
                slot, sid = d.get("slot"), d.get("spool_id")
                STATE["slot_map"] = {k: v for k, v in STATE["slot_map"].items() if v != sid or not sid}
                if sid:
                    STATE["slot_map"][slot] = sid
                else:
                    STATE["slot_map"].pop(slot, None)
                save()
                return None
            if path == "/api/token":
                tok = str(d.get("token") or "").strip().strip('"')
                STATE["token"] = tok
                STATE["cloud_error"] = None
                if tok:
                    STATE["settings"]["mode"] = "cloud"
                save()
            elif path == "/api/token/detect":
                tok = detect_token()
                if not tok:
                    raise ValueError("Tarayıcıda Anycubic oturumu bulunamadı. cloud-universe.anycubic.com'a "
                                     "giriş yapıp tekrar dene (Chrome/Brave/Edge/Arc).")
                STATE["token"] = tok
                STATE["cloud_error"] = None
                STATE["settings"]["mode"] = "cloud"
                save()
            elif path in ("/api/order", "/api/purchase", "/api/product"):
                coll = STATE.setdefault({"/api/order": "orders", "/api/purchase": "purchases", "/api/product": "products"}[path], [])
                o = next((x for x in coll if x["id"] == d.get("id")), None) if d.get("id") else None
                prev = o.get("status") if o else None
                if not o:
                    o = {"id": uuid.uuid4().hex[:10], "created": time.time()}
                    coll.insert(0, o)
                for k, v in d.items():
                    if k not in ("id", "created", "received_spools"):
                        o[k] = v
                if path != "/api/product":
                    o.setdefault("status", "bekliyor" if path == "/api/order" else "siparis")
                msg = None
                if path == "/api/order" and STATE["settings"].get("order_deduct", True):
                    if o["status"] == "basiliyor" and prev != "basiliyor":
                        msg = reserve_order(o)
                    elif prev == "basiliyor" and o["status"] in ("bekliyor", "iptal"):
                        msg = unreserve_order(o["id"])
                save()
                return {"ok": True, "id": o["id"], "msg": msg}
            elif path in ("/api/order/delete", "/api/purchase/delete", "/api/product/delete"):
                key = "orders" if "order" in path else "products" if "product" in path else "purchases"
                if key == "orders":
                    od = next((x for x in STATE["orders"] if x["id"] == d.get("id")), None)
                    if od and od.get("status") not in ("hazir", "teslim"):
                        unreserve_order(od["id"])
                STATE[key] = [x for x in STATE[key] if x["id"] != d.get("id")]
                if key == "orders":
                    for h in STATE["history"]:
                        if h.get("order_id") == d.get("id"):
                            h["order_id"] = None
                save()
                return None
            elif path == "/api/purchase/receive":
                pu = next((x for x in STATE["purchases"] if x["id"] == d.get("id")), None)
                if not pu:
                    raise ValueError("alım yok")
                if pu.get("received_spools"):
                    raise ValueError("Bu alım zaten stoğa eklendi")
                items = pu.get("items") or []
                qty_total = sum(int(i.get("qty") or 0) for i in items) or 1
                ship_each = float(pu.get("shipping") or 0) / qty_total
                made, base_t = [], time.time()
                for it in items:
                    for _ in range(int(it.get("qty") or 0)):
                        sp = {"id": uuid.uuid4().hex[:10], "created": base_t + len(made) * 0.001,
                              "brand": it.get("brand", ""), "material": it.get("material") or "PLA",
                              "name": it.get("name", ""), "color": it.get("color") or "#888888",
                              "total_g": float(it.get("weight_g") or 1000),
                              "price": round(float(it.get("unit_price") or 0) + ship_each, 2),
                              "notes": f"{pu.get('vendor') or 'Alım'} · {time.strftime('%d.%m.%Y')}",
                              "purchase_id": pu["id"]}
                        sp["remaining_g"] = sp["total_g"]
                        STATE["spools"].append(sp)
                        made.append(sp["id"])
                pu["status"], pu["received_spools"], pu["received_at"] = "teslim", made, time.time()
                log(f"Alım teslim alındı: {pu.get('vendor') or '—'} · {len(made)} makara stoğa eklendi")
                save()
                return None
            elif path == "/api/history/order":
                for h in STATE["history"]:
                    if h.get("ts") == d.get("ts") and h.get("kind") != "reserve":
                        h["order_id"] = d.get("order_id") or None
                        if h["order_id"] and h.get("reconciled") != h["order_id"]:
                            reconcile(h["order_id"], h.get("per_spool") or {})
                            h["reconciled"] = h["order_id"]
                save()
                return None
            elif path == "/api/track/order":
                t = STATE.get("track")
                if t:
                    new = d.get("order_id") or None
                    if new and new != t.get("order_id") and STATE["settings"]["auto_deduct"]:
                        reconcile(new, {k: v for k, v in t["per_spool"].items() if spool_by_id(k)})
                    t["order_id"] = new
                    save()
                return None
            elif path == "/api/lan/test":
                host = str(d.get("host") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
                    raise ValueError("Geçerli bir IP adresi gir")
                try:
                    info = lanmod.probe(host)
                except lanmod.LanError as e:
                    raise ValueError(str(e)) from e
                return {"ok": True, "name": info.get("modelName") or info.get("deviceName"), "fw": info.get("version")}
            elif path == "/api/printer":
                STATE["printer_id"] = d.get("id")
                save()
            elif path == "/api/settings":
                STATE["settings"].update({k: d[k] for k in ("low_g", "auto_deduct", "mode", "lan_host", "kwh_price",
                                                            "printer_watt", "labor_hour", "margin", "currency", "default_kg", "order_deduct") if k in d})
                if "mode" in d or "lan_host" in d:
                    STATE["live"] = None
                save()
                wake.set()
                return None
            elif path == "/api/history/delete":
                h = next((h for h in STATE["history"] if h.get("ts") == d.get("ts")), None)
                if h and d.get("restore"):
                    for sid, gr in (h.get("per_spool") or {}).items():
                        sp = spool_by_id(sid)
                        if sp:
                            sp["remaining_g"] = min(sp["total_g"], sp["remaining_g"] + gr)
                STATE["history"] = [x for x in STATE["history"] if x is not h]
                save()
                return None
            elif path == "/api/print/manual":
                per = {}
                for it in d.get("items") or []:
                    sp, gr = spool_by_id(it.get("spool_id")), float(it.get("grams") or 0)
                    if not sp or gr <= 0:
                        continue
                    sp["remaining_g"] = max(0.0, sp["remaining_g"] - gr)
                    per[sp["id"]] = per.get(sp["id"], 0) + gr
                if not per:
                    raise ValueError("En az bir makara ve gram gir")
                now = time.time()
                hours = float(d["hours"]) if d.get("hours") not in (None, "") else None
                STATE["history"].insert(0, finalize_entry({
                    "ts": now, "start": now, "file": str(d.get("file") or "Elle kayıt"), "task_id": None,
                    "grams": round(sum(per.values()), 1), "per_spool": {k: round(v, 1) for k, v in per.items()},
                    "progress": None, "manual": True, "hours": hours, "order_id": d.get("order_id") or None,
                    "reconciled": d.get("order_id") or None}))
                reconcile(d.get("order_id"), per)
                log(f"Elle baskı kaydı: {d.get('file') or '—'} · {sum(per.values()):.1f} g")
                save()
                return None
            elif path != "/api/refresh":
                raise ValueError("bilinmeyen istek")
        # token/yenileme: hemen bir sorgu yap
        if not STATE.get("token") or STATE["settings"].get("mode") != "cloud":
            with lock:
                STATE["cloud_error"] = None
                STATE["live"] = None
            return None
        try:
            poll_once()
        except CloudError as e:
            with lock:
                STATE["cloud_error"] = str(e)
        return None


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        srv = ThreadingHTTPServer((HOST, PORT), H)
    except OSError:
        print(f"Zaten çalışıyor olabilir: http://{HOST}:{PORT}")
        webbrowser.open(f"http://{HOST}:{PORT}")
        return
    threading.Thread(target=poller, daemon=True).start()
    url = f"http://{HOST}:{PORT}"
    print(f"Filament Takip çalışıyor → {url}  (kapatmak için Ctrl+C)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
