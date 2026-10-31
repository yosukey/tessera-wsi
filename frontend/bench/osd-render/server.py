"""Synthetic Deep Zoom server for the OSD rendering-settings bench.

Serves a 100k x 80k px DZI pyramid (256 px tiles, overlap 1, like Tessera's
defaults) whose tiles are solid colours that encode their level in the red
channel, so a canvas readback tells which level each screen pixel came from.

Tiles carry an ETag and, by default, Cache-Control: no-cache, so a re-fetch
of an evicted tile costs a conditional request (304); /__config?cc=<value>
changes the tiles' Cache-Control (pass `private, max-age=86400` to match
what Tessera sends). A fixed per-request latency stands in for network RTT +
nginx (set it with /__config?latency=ms).

usage: python3 server.py [port=8765] [path/to/openseadragon.js]
"""
import io
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
# The OpenSeadragon build the app ships (frontend/node_modules after npm ci).
OSD = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
    ROOT, "..", "..", "node_modules", "openseadragon", "build", "openseadragon", "openseadragon.js")
W, H, TS, OV = 100_000, 80_000, 256, 1
MAXLEVEL = (max(W, H) - 1).bit_length()  # ceil(log2)

state = {"latency_ms": 30.0, "tile_cache_control": "no-cache"}
log: list = []
lock = threading.Lock()
png_cache: dict = {}


def level_color(level: int):
    return (10 + level * 12, 200, 60)


def tile_png(level: int, col: int, row: int) -> bytes | None:
    scale = 2 ** (MAXLEVEL - level)
    lw = -(-W // scale)
    lh = -(-H // scale)
    x0 = col * TS
    y0 = row * TS
    if x0 >= lw or y0 >= lh:
        return None
    w = min(TS + (OV if x0 + TS < lw else 0), lw - x0) + (OV if col > 0 else 0)
    h = min(TS + (OV if y0 + TS < lh else 0), lh - y0) + (OV if row > 0 else 0)
    key = (level, w, h)
    if key not in png_cache:
        buf = io.BytesIO()
        Image.new("RGB", (w, h), level_color(level)).save(buf, "PNG")
        png_cache[key] = buf.getvalue()
    return png_cache[key]


DZI = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" Format="png" Overlap="{OV}" TileSize="{TS}">'
    f'<Size Width="{W}" Height="{H}"/></Image>'
)
TILE_RE = re.compile(r"^/slide_files/(\d+)/(\d+)_(\d+)\.png$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/plain", headers=None):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        if ctype:
            self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path == "/__config":
            for part in q.split("&"):
                if part.startswith("latency="):
                    state["latency_ms"] = float(part.split("=", 1)[1])
                elif part.startswith("cc="):
                    state["tile_cache_control"] = unquote(part.split("=", 1)[1])
            return self._send(200, json.dumps(state).encode(), "application/json")
        if path == "/__mark":
            with lock:
                return self._send(200, json.dumps({"n": len(log)}).encode(), "application/json",
                                  {"Cache-Control": "no-store"})
        if path == "/__log":
            since = int(q.split("=", 1)[1]) if q.startswith("since=") else 0
            with lock:
                body = json.dumps(log[since:]).encode()
            return self._send(200, body, "application/json", {"Cache-Control": "no-store"})
        if path == "/harness.html":
            with open(os.path.join(ROOT, "harness.html"), "rb") as f:
                return self._send(200, f.read(), "text/html", {"Cache-Control": "no-store"})
        if path == "/osd.js":
            with open(OSD, "rb") as f:
                return self._send(200, f.read(), "application/javascript")
        if path == "/slide.dzi":
            return self._send(200, DZI.encode(), "application/xml", {"Cache-Control": "no-cache"})
        m = TILE_RE.match(path)
        if m:
            level, col, row = map(int, m.groups())
            body = tile_png(level, col, row)
            if body is None:
                return self._send(404)
            etag = f'"t{level}-{col}-{row}"'
            time.sleep(state["latency_ms"] / 1000.0)
            conditional = self.headers.get("If-None-Match") == etag
            with lock:
                log.append({"t": time.time(), "l": level, "c": col, "r": row, "s": 304 if conditional else 200})
            cc = state["tile_cache_control"]
            if conditional:
                return self._send(304, b"", None, {"ETag": etag, "Cache-Control": cc})
            return self._send(200, body, "image/png", {"ETag": etag, "Cache-Control": cc})
        self._send(404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"serving on {port}, maxLevel={MAXLEVEL}", flush=True)
    srv.serve_forever()
