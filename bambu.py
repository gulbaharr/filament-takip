"""Bambu Lab yazıcılara (X1 / P1 / A1 / H2) yerel ağdan bağlantı.

Sadece standart kütüphane.
- Durum: mqtts://<ip>:8883, kullanıcı "bblp", şifre = yazıcının LAN erişim kodu,
  konu device/<seri no>/report ("print" nesnesi: gcode_state, mc_percent, ams ...).
  P1/A1 sadece değişenleri yollar; bu yüzden gelen parçalar birleştirilir ve arada "pushall" istenir.
- Gram: yazıcının belleğindeki .gcode.3mf FTPS (örtük TLS, port 990) ile indirilir,
  Metadata/slice_info.config içindeki filament used_g değerleri okunur.
"""
from __future__ import annotations

import ftplib
import io
import json
import re
import socket
import ssl
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

from lan import LanClient, _pkt, _s

ACTIVE = {"RUNNING", "PREPARE", "PAUSE", "SLICING", "INIT"}
PUSHALL_EVERY = 300  # sn; P1 serisi sık "pushall" istenmesini sevmiyor
MQTT_PORT, FTP_PORT = 8883, 990


class BambuError(Exception):
    pass


def _ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # yazıcı kendi imzaladığı sertifikayı kullanır
    return ctx


# --- MQTT --------------------------------------------------------------------
def _connect(host: str, serial: str, code: str):
    try:
        raw = socket.create_connection((host, MQTT_PORT), timeout=10)
    except OSError as e:
        raise BambuError(f"Yazıcıya ulaşılamadı ({e})") from e
    sock = _ctx().wrap_socket(raw)
    cid = "ft-" + uuid.uuid4().hex[:12]
    sock.sendall(_pkt(0x10, _s("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60) + _s(cid) + _s("bblp") + _s(code)))
    kind, body = LanClient._read(sock)
    if kind >> 4 != 2 or len(body) < 2:
        raise BambuError("MQTT bağlantısı kurulamadı")
    if body[1] in (4, 5):
        raise BambuError("Erişim kodu yanlış")
    if body[1] != 0:
        raise BambuError(f"Yazıcı bağlantıyı reddetti (kod {body[1]})")
    sock.sendall(_pkt(0x82, struct.pack("!H", 1) + _s(f"device/{serial}/report") + b"\x00"))
    return sock


def _pushall(sock, serial: str):
    msg = {"pushing": {"sequence_id": str(int(time.time())), "command": "pushall", "version": 1, "push_target": 1}}
    sock.sendall(_pkt(0x30, _s(f"device/{serial}/request") + json.dumps(msg).encode()))


def _publishes(sock):
    """Gelen PUBLISH paketlerinden (konu, json) üretir; zaman aşımında None verir."""
    while True:
        try:
            kind, body = LanClient._read(sock)
        except socket.timeout:
            yield None
            continue
        if kind >> 4 != 3:
            continue
        tl = struct.unpack("!H", body[:2])[0]
        off = 2 + tl + (2 if (kind >> 1) & 3 else 0)
        try:
            yield body[2:2 + tl].decode(errors="replace"), json.loads(body[off:].decode())
        except ValueError:
            continue


def probe(host: str, serial: str, code: str) -> dict:
    """Bağlanıp ilk raporu bekler. Ayarlar'daki 'Test et' için."""
    sock = _connect(host, serial, code)
    try:
        sock.settimeout(2)
        _pushall(sock, serial)
        end = time.time() + 12
        for item in _publishes(sock):
            if item and isinstance(item[1].get("print"), dict):
                pr = item[1]["print"]
                if "gcode_state" in pr or "ams" in pr:
                    trays = sum(len(a.get("tray") or []) for a in (pr.get("ams") or {}).get("ams") or [])
                    return {"state": pr.get("gcode_state"), "trays": trays}
            if time.time() > end:
                raise BambuError("Bağlantı kuruldu ama rapor gelmedi — seri numarası yanlış olabilir")
    finally:
        sock.close()
    raise BambuError("Rapor alınamadı")


# --- FTPS: baskı dosyasından gram ---------------------------------------------
class _ImplicitFTPS(ftplib.FTP_TLS):
    """Bambu'nun FTP sunucusu örtük TLS (990) ve veri kanalında oturum yeniden kullanımı ister."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, session=self.sock.session)
        return conn, size


def _plate_info(data: bytes, plate: int | None) -> dict:
    z = zipfile.ZipFile(io.BytesIO(data))
    root = ET.fromstring(z.read("Metadata/slice_info.config"))
    plates = []
    for p in root.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in p.findall("metadata")}
        fil = [{"g": float(f.get("used_g") or 0), "color": f.get("color"), "type": f.get("type") or "PLA"}
               for f in p.findall("filament") if float(f.get("used_g") or 0) > 0]
        plates.append({"index": int(meta.get("index") or len(plates) + 1), "items": fil,
                       "hours": round(float(meta.get("prediction") or 0) / 3600, 2) or None})
    pick = next((p for p in plates if p["index"] == plate), None) or next((p for p in plates if p["items"]), None)
    if not pick:
        raise BambuError("Dosyada gram bilgisi yok")
    return {"est_g": round(sum(i["g"] for i in pick["items"]), 2), **pick}


def fetch_job(host: str, code: str, name: str, gcode_file: str | None) -> dict:
    ftp = _ImplicitFTPS(context=_ctx(), timeout=20)
    try:
        ftp.connect(host, FTP_PORT)
        ftp.login("bblp", code)
        ftp.prot_p()
        cands = []
        for d in ("/cache", "/", "/model"):
            try:
                cands += [(d.rstrip("/") + "/" + n.rsplit("/", 1)[-1]) for n in ftp.nlst(d)]
            except ftplib.all_errors:
                continue
        base = (name or "").lower()
        files = [c for c in cands if c.lower().endswith(".3mf")]
        exact = [c for c in files if c.rsplit("/", 1)[-1].lower() in (f"{base}.gcode.3mf", f"{base}.3mf")]
        loose = [c for c in files if c.rsplit("/", 1)[-1].lower().startswith(base)] if base else []
        if gcode_file and gcode_file.lower().endswith(".3mf"):
            exact.insert(0, gcode_file if gcode_file.startswith("/") else "/" + gcode_file)
        for path in exact + loose:
            buf = io.BytesIO()
            try:
                ftp.retrbinary(f"RETR {path}", buf.write)
            except ftplib.all_errors:
                continue
            m = re.search(r"plate_(\d+)\.gcode", gcode_file or "")
            return {**_plate_info(buf.getvalue(), int(m.group(1)) if m else None), "path": path}
        raise BambuError(f"Yazıcıda '{name}' dosyası bulunamadı")
    finally:
        try:
            ftp.quit()
        except ftplib.all_errors + (OSError,):
            ftp.close()


# --- Arka plan istemcisi -------------------------------------------------------
class BambuClient(threading.Thread):
    """Yazıcıya bağlı kalır; server.track() ile aynı biçimde canlı durum verir."""

    def __init__(self, host: str, serial: str, code: str, on_update=None, log=print):
        super().__init__(daemon=True)
        self.host, self.serial, self.code = host, serial, code
        self.on_update, self.log = on_update, log
        self.stop_ev = threading.Event()
        self.pr: dict = {}
        self.raw: dict = {}
        self.connected = False
        self.error: str | None = None
        self.updated = None
        self.jobs: dict = {}  # dosya adı → {"est_g", ...} ya da {"error"}

    def stop(self):
        self.stop_ev.set()

    def run(self):
        while not self.stop_ev.is_set():
            try:
                self._session()
            except Exception as e:  # noqa: BLE001
                self.connected = False
                if self.error != str(e):
                    self.log(f"Bambu: {e}")
                self.error = str(e)
            self.stop_ev.wait(20)

    def _session(self):
        sock = _connect(self.host, self.serial, self.code)
        try:
            self.connected, self.error = True, None
            self.log(f"Bambu: yazıcıya bağlanıldı ({self.host})")
            sock.settimeout(2)
            last_push = last_ping = 0.0
            for item in _publishes(sock):
                if self.stop_ev.is_set():
                    break
                now = time.time()
                if now - last_push > PUSHALL_EVERY:
                    _pushall(sock, self.serial)
                    last_push = now
                if now - last_ping > 30:
                    sock.sendall(b"\xc0\x00")
                    last_ping = now
                if item:
                    self._handle(item[1])
        finally:
            self.connected = False
            try:
                sock.close()
            except OSError:
                pass

    def _handle(self, p: dict):
        pr = p.get("print")
        if not isinstance(pr, dict):
            return
        for k, v in pr.items():
            if k == "ams" and isinstance(v, dict):
                self.pr.setdefault("ams", {}).update(v)
            else:
                self.pr[k] = v
        self.raw = {"print": self.pr}
        self.updated = time.time()
        if str(self.pr.get("gcode_state") or "").upper() in ACTIVE:
            self._want_job(self._job_key(), str(self.pr.get("subtask_name") or ""), self.pr.get("gcode_file"))
        if self.on_update:
            self.on_update()

    def _job_key(self) -> str:
        pr = self.pr
        return f"{pr.get('subtask_name') or ''}|{pr.get('gcode_file') or ''}|{pr.get('task_id') or pr.get('subtask_id') or ''}"

    def _want_job(self, key: str, name: str, gfile):
        if not name or key in self.jobs:
            return
        self.jobs[key] = {"pending": True}

        def work():
            try:
                j = fetch_job(self.host, self.code, name, gfile)
                self.log(f"Bambu: '{name}' için {j['est_g']} g okundu")
            except Exception as e:  # noqa: BLE001
                j = {"error": str(e)}
                self.log(f"Bambu: '{name}' gramı okunamadı ({e}); baskıyı bitince elle düzelt")
            self.jobs[key] = j
            if self.on_update:
                self.on_update()
        threading.Thread(target=work, daemon=True).start()

    def live(self) -> dict | None:
        if not self.updated:
            return None
        pr = self.pr
        st = str(pr.get("gcode_state") or "").upper()
        printing = st in ACTIVE
        name = str(pr.get("subtask_name") or "") or None
        tid = str(pr.get("task_id") or pr.get("subtask_id") or "")
        if tid in ("", "0"):
            tid = f"{name}|{pr.get('gcode_start_time') or ''}"
        ams = pr.get("ams") or {}
        slots = []
        for a in ams.get("ams") or []:
            for t in a.get("tray") or []:
                col = str(t.get("tray_color") or "")
                slots.append({"key": f"{a.get('id')}:{t.get('id')}", "box": int(a.get("id") or 0), "index": int(t.get("id") or 0),
                              "material": t.get("tray_type") or "", "color": ("#" + col[:6].lower()) if len(col) >= 6 else None,
                              "sku": t.get("tray_sub_brands") or "", "status": None, "empty": not t.get("tray_type")})
        vt = pr.get("vt_tray")
        if isinstance(vt, dict):
            col = str(vt.get("tray_color") or "")
            slots.append({"key": "ext", "box": -1, "index": 0, "material": vt.get("tray_type") or "",
                          "color": ("#" + col[:6].lower()) if len(col) >= 6 else None, "sku": "", "status": None,
                          "empty": not vt.get("tray_type")})
        loaded = None
        try:
            n = int(ams.get("tray_now", 255))
            loaded = "ext" if n == 254 else None if n == 255 else f"{n // 4}:{n % 4}"
        except (TypeError, ValueError):
            pass
        if loaded is None and not (ams.get("ams") or []):
            loaded = "ext"  # AMS yoksa harici makara
        job = self.jobs.get(self._job_key(), {}) if name else {}
        return {"name": f"Bambu Lab ({self.serial[-4:]})", "model": "Bambu Lab", "online": self.connected,
                "printing": printing, "state": "finished" if st == "FINISH" else (st.lower() or None),
                "task_id": tid if printing else None, "file": name, "progress": pr.get("mc_percent"),
                "remain_min": pr.get("mc_remaining_time"), "est_m": None, "est_g": job.get("est_g"),
                "job_error": job.get("error"), "used_mm": None, "slots": slots, "loaded": loaded,
                "updated": self.updated, "source": "bambu"}
