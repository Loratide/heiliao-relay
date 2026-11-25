# Heiliao HLS relay v2.1: strips the fake JPEG prefix glued in front of each TS
# segment by the bf.qugt.com / bf.zsxsf.com CDN and re-serves clean HLS.
# v2 changes: segments are STREAMED (first byte out as soon as it arrives,
# not after a full download), Range requests are translated and answered
# with a proper 206, HTTP/1.1 keep-alive is on, dead client connections
# are dropped quietly. Seeking depends on all of this.
#   /p?u=<m3u8 url>  -> playlist with segment lines rewritten to /s?u=<seg url>
#   /s?u=<seg url>   -> segment bytes starting at the first TS sync byte
#   /health          -> ok
import os, re, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"}
ALLOWED_SUFFIXES = ("qugt.com", "zsxsf.com")
CHUNK = 65536
OFFSET_CACHE = {}

def allowed(u):
    try:
        host = urllib.parse.urlparse(u).hostname or ""
        return any(host == s or host.endswith("." + s) for s in ALLOWED_SUFFIXES)
    except Exception:
        return False

def fetch(u, rng=None):
    req = urllib.request.Request(u, headers=dict(UA, **({"Range": rng} if rng else {})))
    return urllib.request.urlopen(req, timeout=30)

def ts_offset(data):
    n = len(data)
    for off in range(0, min(4096, max(0, n - 188 * 4))):
        if data[off] == 0x47 and all(data[off + 188 * i] == 0x47 for i in range(4)):
            return off
    return 0

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _headers(self, code, ctype, length=None, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items(): self.send_header(k, v)
        self.end_headers()

    def _send(self, code, ctype, body, extra=None):
        try:
            self._headers(code, ctype, len(body), extra)
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _pump(self, r, skip=0):
        # stream upstream -> client, dropping the first `skip` bytes
        try:
            left = skip
            while True:
                data = r.read(CHUNK)
                if not data:
                    break
                if left:
                    if len(data) <= left:
                        left -= len(data)
                        continue
                    data = data[left:]
                    left = 0
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(q.query)
        u = (params.get("u") or [""])[0]
        if q.path == "/health":
            return self._send(200, "text/plain", b"ok")
        if not u or not allowed(u):
            return self._send(400, "text/plain", b"bad u")
        try:
            if q.path == "/p":
                with fetch(u) as r:
                    text = r.read().decode("utf-8", "replace")
                out = []
                for line in text.splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        out.append("/s?u=" + urllib.parse.quote(urllib.parse.urljoin(u, s), safe=""))
                    elif 'URI="' in line:
                        out.append(re.sub(r'URI="([^"]+)"',
                            lambda m: 'URI="/s?u=' + urllib.parse.quote(urllib.parse.urljoin(u, m.group(1)), safe="") + '"',
                            line))
                    else:
                        out.append(line)
                return self._send(200, "application/vnd.apple.mpegurl", ("\n".join(out) + "\n").encode())
            if q.path == "/s":
                return self._seg(u)
            return self._send(404, "text/plain", b"not found")
        except Exception as e:
            return self._send(502, "text/plain", ("relay error: " + str(e)[:120]).encode())

    def _seg(self, u):
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            # learn this segment's prefix length from its head (cached)
            off = OFFSET_CACHE.get(u)
            if off is None:
                with fetch(u, "bytes=0-8191") as r:
                    off = ts_offset(r.read(8192))
                OFFSET_CACHE[u] = off
            a = int(m.group(1))
            b = m.group(2)
            up = "bytes=%d-%s" % (a + off, (int(b) + off) if b else "")
            with fetch(u, up) as r:
                length = r.headers.get("Content-Length")
                length = int(length) if length else None
                extra = {"Accept-Ranges": "bytes"}
                cr = r.headers.get("Content-Range") or ""
                mm = re.match(r"bytes \d+-\d+/(\d+)", cr)
                if mm and length is not None:
                    total = int(mm.group(1)) - off
                    extra["Content-Range"] = "bytes %d-%d/%d" % (a, a + length - 1, total)
                if length is None:
                    extra["Connection"] = "close"
                    self.close_connection = True
                self._headers(206 if r.status == 206 else 200, "video/mp2t", length, extra)
                self._pump(r)
            return
        with fetch(u) as r:
            total = r.headers.get("Content-Length")
            total = int(total) if total else None
            # peek at the first chunk to find the prefix, then stream the rest
            first = r.read(CHUNK)
            off = ts_offset(first)
            OFFSET_CACHE[u] = off
            length = (total - off) if total is not None else None
            extra = {"Accept-Ranges": "bytes"}
            if length is None:
                extra["Connection"] = "close"
                self.close_connection = True
            self._headers(200, "video/mp2t", length, extra)
            try:
                if len(first) > off:
                    self.wfile.write(first[off:])
                self._pump(r)
            except (BrokenPipeError, ConnectionResetError):
                pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
