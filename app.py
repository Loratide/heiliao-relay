# Heiliao HLS relay: strips the fake JPEG prefix glued in front of each TS
# segment by the bf.zsxsf.com CDN and re-serves clean HLS over plain HTTPS.
# Endpoints:
#   /p?u=<m3u8 url>  -> playlist with segment lines rewritten to /s?u=<seg url>
#   /s?u=<seg url>   -> segment bytes starting at the first TS sync byte
#   /health          -> ok
import os, re, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"}
ALLOWED_SUFFIXES = ("qugt.com", "zsxsf.com")

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
    # first offset where 0x47 repeats every 188 bytes (4 packets deep)
    n = len(data)
    for off in range(0, min(4096, n - 188 * 4)):
        if data[off] == 0x47 and all(data[off + 188 * i] == 0x47 for i in range(4)):
            return off
    return 0

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items(): self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
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
                base = u
                out = []
                for line in text.splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        absu = urllib.parse.urljoin(base, s)
                        out.append("/s?u=" + urllib.parse.quote(absu, safe=""))
                    elif 'URI="' in line:
                        out.append(re.sub(r'URI="([^"]+)"',
                            lambda m: 'URI="/s?u=' + urllib.parse.quote(urllib.parse.urljoin(base, m.group(1)), safe="") + '"',
                            line))
                    else:
                        out.append(line)
                return self._send(200, "application/vnd.apple.mpegurl", ("\n".join(out) + "\n").encode())
            if q.path == "/s":
                with fetch(u, self.headers.get("Range")) as r:
                    data = r.read()
                off = ts_offset(data)
                return self._send(200, "video/mp2t", data[off:])
            return self._send(404, "text/plain", b"not found")
        except Exception as e:
            return self._send(502, "text/plain", ("relay error: " + str(e)[:120]).encode())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
