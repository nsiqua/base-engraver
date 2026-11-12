"""Tiny local web server for the urn engraver (Python standard library only).

Run:   python server.py      then open http://localhost:8000
"""
import importlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import engraver

HOST, PORT = "127.0.0.1", 8000
INDEX = Path(__file__).with_name("index.html")
ENGR_FILE = Path(__file__).with_name("engraver.py")
_engr_mtime = ENGR_FILE.stat().st_mtime


def _current_engraver():
    """Reload engraver.py if the file changed on disk, preserving the loaded Base in memory."""
    global _engr_mtime
    mtime = ENGR_FILE.stat().st_mtime
    if mtime != _engr_mtime:
        cached_base = engraver._base
        importlib.reload(engraver)
        if cached_base is not None:
            cached_base.__class__ = engraver.Base
            if not hasattr(cached_base, "prof_z"):
                v = cached_base.manifold.to_mesh().vert_properties[:, :3].astype("float64")
                r = (v[:, 0] ** 2 + v[:, 1] ** 2) ** 0.5
                cached_base._outer_profile(v[:, 2], r)
            if not hasattr(cached_base, "orig_height"):
                cached_base.orig_height = cached_base.z_max - cached_base.z_min
            cached_base.preview = cached_base._preview_shape()
            if not hasattr(cached_base, "has_cat"):
                cached_base.has_cat = engraver.CAT_STL.exists()
                if cached_base.has_cat:
                    cached_base._load_cat(engraver.CAT_STL, cached_base.manifold.num_tri())
        engraver._base = cached_base
        _engr_mtime = mtime
        print("[server] reloaded engraver.py")
    return engraver


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        self._send(code, json.dumps({"error": msg}).encode(), "application/json")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            eng = _current_engraver()
            ready = eng._base is not None
            has_cat = bool(ready and getattr(eng._base, "has_cat", False))
            self._send(200, json.dumps({
                "ready": ready,
                "hasCat": has_cat,
                "maxChars": eng.MAX_CHARS,
                "defaultWeightKg": eng.DEFAULT_WEIGHT_KG,
                "minWeightKg": eng.MIN_WEIGHT_KG,
                "maxWeightKg": eng.MAX_WEIGHT_KG,
                "mlPerKg": eng.ML_PER_KG,
                "origCapacityMl": eng.ORIG_CAPACITY_ML,
            }).encode(), "application/json")
        else:
            self._error(404, "Not found")

    def do_POST(self):
        if self.path not in ("/api/preview", "/api/engrave"):
            return self._error(404, "Not found")
        eng = _current_engraver()
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            text = payload.get("text", "")
            model = payload.get("model", "cat")
            if model not in ("cat", "base"):
                model = "cat"
            weight_kg = payload.get("weightKg", eng.DEFAULT_WEIGHT_KG)
            name, data, sizing = eng.engrave(
                text, preview=self.path == "/api/preview", model=model, weight_kg=weight_kg
            )
        except eng.EngraveError as e:
            return self._error(400, str(e))
        except Exception as e:  # unexpected
            print("[error]", repr(e))
            return self._error(500, f"Engraving failed: {e}")
        prefix = "Cat_Urn" if model == "cat" else "Base"
        w_tag = f"{sizing['weightKg']:g}kg"
        filename = f"{prefix}_{name.replace(' ', '_')}_{w_tag}.stl"
        self._send(200, data, "model/stl", {
            "Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(filename),
            "X-Text": urllib.parse.quote(name),
            "X-Model": model,
            "X-Weight-Kg": str(sizing["weightKg"]),
            "X-Capacity-Ml": str(sizing["capacityMl"]),
            "X-Scale": f"{sizing['scale']:.4f}",
        })

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    # load the base and cat models in the background so the page opens immediately
    threading.Thread(target=engraver.get_base, daemon=True).start()
    print(f"Urn engraver running at http://localhost:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
