"""Tiny local web server for the base engraver (Python standard library only).

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
            cached_base.preview = cached_base._preview_shape()
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
            ready = engraver._base is not None
            self._send(200, json.dumps({"ready": ready, "maxChars": engraver.MAX_CHARS}).encode(),
                       "application/json")
        else:
            self._error(404, "Not found")

    def do_POST(self):
        if self.path not in ("/api/preview", "/api/engrave"):
            return self._error(404, "Not found")
        try:
            length = int(self.headers.get("Content-Length", 0))
            text = json.loads(self.rfile.read(length) or b"{}").get("text", "")
            eng = _current_engraver()
            name, data = eng.engrave(text, preview=self.path == "/api/preview")
        except eng.EngraveError as e:
            return self._error(400, str(e))
        except Exception as e:  # unexpected
            print("[error]", repr(e))
            return self._error(500, f"Engraving failed: {e}")
        filename = f"Base_{name.replace(' ', '_')}.stl"
        self._send(200, data, "model/stl", {
            "Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(filename),
            "X-Text": urllib.parse.quote(name),
        })

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    # load the large base in the background so the page opens immediately
    threading.Thread(target=engraver.get_base, daemon=True).start()
    print(f"Base engraver running at http://localhost:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
