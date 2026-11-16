"""Kobra S1 (ve benzeri) yazıcıya yerel ağdan (LAN Modu) bağlantı.

Sadece standart kütüphane + macOS'taki /usr/bin/openssl kullanır.
Protokol: chrisfore/anycubic_ha_local (MIT) belgeledi; burada bağımsız bir uygulama.

1. GET  http://<ip>:18910/info           → token, ctrlInfoUrl, modelId
2. POST <ctrlInfoUrl>?ts&nonce&sign&did   → şifreli broker bilgisi
3. AES-128-CBC çöz (anahtar token[16:32], iv = cevap token'ı) → MQTT kullanıcı/şifre
4. mqtts://<ip>:9883 → anycubic/anycubicCloud/v1/printer/public/<model>/<device>/# dinle
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import socket
import ssl
import string
import struct
import subprocess
import threading
import time
import urllib.request
import uuid

PREFIX = "anycubic/anycubicCloud/v1"
QUERY_INTERVAL = 15
QUERIES = {"info": "query", "print": "query", "multiColorBox": "getInfo", "tempature": "query"}
ACTIVE_STATES = {"printing", "preheating", "downloading", "checking", "paused", "pausing",
                 "resuming", "resumed", "busy", "updated"}


class LanError(Exception):
    pass


def _http_json(method: str, url: str) -> dict:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())
    except OSError as e:
        raise LanError(f"Yazıcıya ulaşılamadı ({e})") from e
    except ValueError as e:
        raise LanError("Yazıcıdan anlaşılmayan cevap") from e


def _aes_cbc_decrypt(b64: str, key: bytes, iv: bytes) -> bytes:
    p = subprocess.run(["/usr/bin/openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()],
                       input=base64.b64decode(b64), capture_output=True, timeout=10)
    if p.returncode != 0:
        raise LanError("Yazıcı cevabı çözülemedi")
    return p.stdout


def probe(host: str) -> dict:
    """Yazıcı LAN modunda mı? /info belgesini döndürür."""
    info = _http_json("GET", f"http://{host}:18910/info")
    if info.get("ctrlType") == "cloud":
        raise LanError("Yazıcı bulut modunda. Yazıcı ekranından LAN Modu'nu aç.")
    if not all(info.get(k) for k in ("token", "ctrlInfoUrl", "modelId")):
        raise LanError("Bu yazıcı modeli desteklenmiyor")
    return info


def handshake(host: str) -> dict:
    info = probe(host)
    token = str(info["token"])
    ts = int(time.time() * 1000)
    nonce = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(6))
    did = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(32))
    keyed = hashlib.md5(token[:16].encode()).hexdigest()
    sign = hashlib.md5(f"{keyed}{ts}{nonce}".encode()).hexdigest()
    ctrl = _http_json("POST", f"{info['ctrlInfoUrl']}?ts={ts}&nonce={nonce}&sign={sign}&did={did}")
    if ctrl.get("code") != 200 or not isinstance(ctrl.get("data"), dict):
        raise LanError(f"Yazıcı isteği reddetti: {ctrl.get('message')}")
    d = ctrl["data"]
    iv = str(d["token"]).encode()[:16].ljust(16, b"\0")
    broker = json.loads(_aes_cbc_decrypt(str(d["info"]), token[16:32].encode(), iv).decode())
    m = re.match(r"^mqtts?://([^:/]+)(?::(\d+))?", str(broker.get("broker", "")))
    if not m:
        raise LanError("Broker bilgisi eksik")
    return {"host": m.group(1), "port": int(m.group(2) or 9883), "username": broker["username"],
            "password": broker["password"], "device_id": broker["deviceId"], "model_id": str(info["modelId"]),
            "name": info.get("modelName") or info.get("deviceName") or "Anycubic"}


# --- Minimal MQTT 3.1.1 istemcisi (QoS 0) -----------------------------------
def _s(b: bytes | str) -> bytes:
    b = b.encode() if isinstance(b, str) else b
    return struct.pack("!H", len(b)) + b


def _pkt(kind: int, body: bytes) -> bytes:
    n, rl = len(body), b""
    while True:
        byte, n = n % 128, n // 128
        rl += bytes([byte | (0x80 if n else 0)])
        if not n:
            break
    return bytes([kind]) + rl + body


class LanClient(threading.Thread):
    """Arka planda yazıcıya bağlı kalır, son durumu `self.state`te tutar."""

    def __init__(self, host: str, on_update=None, log=print):
        super().__init__(daemon=True)
        self.host, self.on_update, self.log = host, on_update, log
        self.stop_ev = threading.Event()
        self.state: dict = {"print": None, "boxes": None, "info": None, "temps": None}
        self.raw: dict = {}
        self.connected = False
        self.error: str | None = None
        self.name_ = None
        self.updated = None

    def stop(self):
        self.stop_ev.set()

    def run(self):
        while not self.stop_ev.is_set():
            try:
                self._session()
            except Exception as e:  # noqa: BLE001
                self.connected = False
                if self.error != str(e):
                    self.log(f"LAN: {e}")
                self.error = str(e)
            self.stop_ev.wait(20)

    def _session(self):
        b = handshake(self.host)
        self.name_ = b["name"]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((b["host"], b["port"]), timeout=10)
        sock = ctx.wrap_socket(raw)
        try:
            cid = "ft-" + uuid.uuid4().hex[:12]
            sock.sendall(_pkt(0x10, _s("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
                              + _s(cid) + _s(b["username"]) + _s(b["password"])))
            kind, body = self._read(sock)
            if kind >> 4 != 2 or len(body) < 2 or body[1] != 0:
                raise LanError("MQTT bağlantısı reddedildi")
            report = f"{PREFIX}/printer/public/{b['model_id']}/{b['device_id']}/#"
            sock.sendall(_pkt(0x82, struct.pack("!H", 1) + _s(report) + b"\x00"))
            self.connected, self.error = True, None
            self.log(f"LAN: {b['name']} yazıcısına bağlanıldı ({self.host})")
            qbase = f"{PREFIX}/web/printer/{b['model_id']}/{b['device_id']}/"
            last_q = last_ping = 0.0
            sock.settimeout(2)
            while not self.stop_ev.is_set():
                now = time.time()
                if now - last_q > QUERY_INTERVAL:
                    for t, a in QUERIES.items():
                        msg = {"type": t, "action": a, "timestamp": int(now * 1000), "msgid": str(uuid.uuid4()), "data": {}}
                        sock.sendall(_pkt(0x30, _s(qbase + t) + json.dumps(msg).encode()))
                    last_q = now
                if now - last_ping > 30:
                    sock.sendall(b"\xc0\x00")
                    last_ping = now
                try:
                    kind, body = self._read(sock)
                except socket.timeout:
                    continue
                if kind >> 4 == 3:
                    tl = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + tl].decode(errors="replace")
                    off = 2 + tl + (2 if (kind >> 1) & 3 else 0)
                    self._handle(topic, body[off:])
        finally:
            self.connected = False
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _read(sock) -> tuple[int, bytes]:
        def rx(n):
            buf = b""
            while len(buf) < n:
                c = sock.recv(n - len(buf))
                if not c:
                    raise LanError("Bağlantı kapandı")
                buf += c
            return buf
        kind = rx(1)[0]
        mult, n = 1, 0
        while True:
            byte = rx(1)[0]
            n += (byte & 0x7F) * mult
            mult *= 128
            if not byte & 0x80:
                break
        return kind, rx(n) if n else b""

    def _handle(self, topic: str, payload: bytes):
        try:
            p = json.loads(payload.decode())
        except ValueError:
            return
        t = p.get("type") or topic.rsplit("/", 1)[-1]
        self.raw[t] = p
        d = p.get("data") or {}
        if t == "print":
            st = p.get("state")
            if isinstance(d, dict):
                self.state["print"] = {"state": st, **d}
        elif t == "multiColorBox":
            boxes = d.get("multi_color_box") if isinstance(d, dict) else None
            if boxes is not None:
                self.state["boxes"] = boxes if isinstance(boxes, list) else [boxes]
        elif t == "info":
            self.state["info"] = d
        elif t == "tempature":
            self.state["temps"] = d
        self.updated = time.time()
        if self.on_update:
            self.on_update()

    def live(self) -> dict | None:
        """server.parse_info ile aynı biçimde canlı durum."""
        if not self.updated:
            return None
        pr = self.state["print"] or {}
        st = str(pr.get("state") or "").lower()
        printing = st in ACTIVE_STATES
        su = pr.get("supplies_usage")
        slots, loaded = [], None
        for bi, box in enumerate(self.state["boxes"] or []):
            ls = box.get("loaded_slot", -1)
            for s in box.get("slots") or []:
                key = f"{bi}:{s.get('index')}"
                col = s.get("color")
                hexc = "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in col[:3]) if isinstance(col, list) and len(col) >= 3 else None
                slots.append({"key": key, "box": bi, "index": s.get("index"), "material": s.get("type") or "",
                              "color": hexc, "sku": s.get("sku") or "", "status": s.get("status"),
                              "empty": col == -2 or not s.get("type")})
                if ls is not None and ls >= 0 and s.get("index") == ls:
                    loaded = key
        info = self.state["info"] or {}
        return {"name": info.get("printerName") or self.name_, "model": self.name_, "online": self.connected,
                "printing": printing, "state": st or None, "task_id": pr.get("taskid") if printing else None,
                "file": pr.get("filename"), "progress": pr.get("progress"), "remain_min": pr.get("remain_time"),
                "est_m": (float(su) / 1000.0) if isinstance(su, (int, float)) and su > 0 else None, "used_mm": None,
                "slots": slots, "loaded": loaded, "updated": self.updated, "source": "lan"}
