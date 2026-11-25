# Heiliao HLS relay v2.3: strips the fake JPEG prefix glued in front of each
# TS segment by the bf.qugt.com / bf.zsxsf.com CDN and re-serves clean HLS.
# v2.2: persistent upstream connections (no per-segment TLS setup), an
# in-memory segment cache (repeat / seek-back requests are instant), /s
# responses are phone-cacheable, shorter upstream timeouts.
# v2.1: responses without a known length are closed properly.
# v2:   segments are streamed; Range requests get a proper 206.
#   /p?u=<m3u8 url>  -> playlist with segment lines rewritten to /s?u=<seg url>
#   /s?u=<seg url>   -> segment bytes starting at the first TS sync byte
#   /health          -> ok
import os, re, ssl, threading, http.client, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"}
ALLOWED_SUFFIXES = ("qugt.com", "zsxsf.com")
CHUNK = 65536
OFFSET_CACHE = {}
SEG_CACHE = {}
SEG_ORDER = []
SEG_BYTES = [0]
SEG_MAX = 48 * 1024 * 1024
LOCK = threading.Lock()
PLAYLISTS = {}
SEG_INDEX = {}
INFLIGHT = set()
PREFETCH_SEM = threading.Semaphore(3)
_local = threading.local()
CTX = ssl.create_default_context()

def allowed(u):
    try:
        host = urllib.parse.urlparse(u).hostname or ""
        return any(host == s or host.endswith("." + s) for s in ALLOWED_SUFFIXES)
    except Exception:
        return False

def _conn(host, port, https):
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = {}
        _local.pool = pool
    key = (host, port, https)
    c = pool.get(key)
    if c is None:
        cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
        c = cls(host, port, timeout=12, context=CTX) if https else cls(host, port, timeout=12)
        pool[key] = c
    return c

def fetch(u, rng=None):
    # returns an http.client response (streaming); follows redirects
    for hop in range(4):
        p = urllib.parse.urlparse(u)
        https = p.scheme == "https"
        port = p.port or (443 if https else 80)
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        headers = dict(UA)
        if rng:
            headers["Range"] = rng
        last = None
        for attempt in range(2):
            c = _conn(p.hostname, port, https)
            try:
                c.request("GET", path, headers=headers)
                r = c.getresponse()
                last = r
                break
            except Exception:
                try: c.close()
                except Exception: pass
                pool = getattr(_local, "pool", {})
                pool.pop((p.hostname, port, https), None)
                last = None
        if last is None:
            raise IOError("upstream connect failed")
        if last.status in (301, 302, 303, 307, 308):
            loc = last.getheader("Location")
            try: last.read()
            except Exception: pass
            if loc:
                u = urllib.parse.urljoin(u, loc)
                continue
        return last
    raise IOError("too many redirects")

def ts_offset(data):
    n = len(data)
    for off in range(0, min(4096, max(0, n - 188 * 4))):
        if data[off] == 0x47 and all(data[off + 188 * i] == 0x47 for i in range(4)):
            return off
    return 0

def cache_get(u):
    with LOCK:
        return SEG_CACHE.get(u)

def cache_put(u, data):
    if len(data) > 4 * 1024 * 1024:
        return
    with LOCK:
        if u in SEG_CACHE:
            return
        while SEG_ORDER and SEG_BYTES[0] + len(data) > SEG_MAX:
            old = SEG_ORDER.pop(0)
            SEG_BYTES[0] -= len(SEG_CACHE.pop(old, b""))
        SEG_CACHE[u] = data
        SEG_ORDER.append(u)
        SEG_BYTES[0] += len(data)

def _prefetch_one(target):
    try:
        r = fetch(target)
        data = r.read()
        off = ts_offset(data)
        OFFSET_CACHE[target] = off
        if off < len(data):
            cache_put(target, data[off:])
    except Exception:
        pass
    finally:
        with LOCK:
            INFLIGHT.discard(target)
        PREFETCH_SEM.release()

def schedule_prefetch(u):
    with LOCK:
        hit = SEG_INDEX.get(u)
        if not hit:
            return
        pl, idx = hit
        lst = PLAYLISTS.get(pl) or []
        todo = []
        for j in (idx + 1, idx + 2, idx + 3):
            if j < len(lst):
                t = lst[j]
                if t not in SEG_CACHE and t not in INFLIGHT:
                    INFLIGHT.add(t)
                    todo.append(t)
    for t in todo:
        if not PREFETCH_SEM.acquire(blocking=False):
            with LOCK:
                INFLIGHT.discard(t)
            continue
        threading.Thread(target=_prefetch_one, args=(t,), daemon=True).start()

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _headers(self, code, ctype, length=None, extra=None, cacheable=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400" if cacheable else "no-store")
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

    def _serve_range_from_cache(self, data):
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if not m:
            self._headers(200, "video/mp2t", len(data), {"Accept-Ranges": "bytes"}, cacheable=True)
            try: self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError): pass
            return
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else len(data) - 1
        b = min(b, len(data) - 1)
        part = data[a:b + 1]
        extra = {"Accept-Ranges": "bytes",
                 "Content-Range": "bytes %d-%d/%d" % (a, b, len(data))}
        self._headers(206, "video/mp2t", len(part), extra, cacheable=True)
        try: self.wfile.write(part)
        except (BrokenPipeError, ConnectionResetError): pass

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
                r = fetch(u)
                text = r.read().decode("utf-8", "replace")
                out = []
                raw_segs = []
                for line in text.splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        full = urllib.parse.urljoin(u, s)
                        raw_segs.append(full)
                        out.append("/s?u=" + urllib.parse.quote(full, safe=""))
                    elif 'URI="' in line:
                        out.append(re.sub(r'URI="([^"]+)"',
                            lambda mm: 'URI="/s?u=' + urllib.parse.quote(urllib.parse.urljoin(u, mm.group(1)), safe="") + '"',
                            line))
                    else:
                        out.append(line)
                with LOCK:
                    PLAYLISTS[u] = raw_segs
                    for i, su in enumerate(raw_segs):
                        SEG_INDEX[su] = (u, i)
                    while len(PLAYLISTS) > 4:
                        old_pl = next(iter(PLAYLISTS))
                        for su in PLAYLISTS.pop(old_pl):
                            SEG_INDEX.pop(su, None)
                return self._send(200, "application/vnd.apple.mpegurl", ("\n".join(out) + "\n").encode())
            if q.path == "/s":
                return self._seg(u)
            return self._send(404, "text/plain", b"not found")
        except Exception as e:
            return self._send(502, "text/plain", ("relay error: " + str(e)[:120]).encode())

    def _seg(self, u):
        schedule_prefetch(u)
        cached = cache_get(u)
        if cached is not None:
            return self._serve_range_from_cache(cached)
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            off = OFFSET_CACHE.get(u)
            if off is None:
                r0 = fetch(u, "bytes=0-8191")
                off = ts_offset(r0.read(8192))
                OFFSET_CACHE[u] = off
            a = int(m.group(1))
            b = m.group(2)
            up = "bytes=%d-%s" % (a + off, (int(b) + off) if b else "")
            r = fetch(u, up)
            length = r.getheader("Content-Length")
            length = int(length) if length else None
            extra = {"Accept-Ranges": "bytes"}
            cr = r.getheader("Content-Range") or ""
            mm = re.match(r"bytes \d+-\d+/(\d+)", cr)
            if mm and length is not None:
                extra["Content-Range"] = "bytes %d-%d/%d" % (a, a + length - 1, int(mm.group(1)) - off)
            if length is None:
                extra["Connection"] = "close"
                self.close_connection = True
            self._headers(206 if r.status == 206 else 200, "video/mp2t", length, extra, cacheable=True)
            self._pump(r)
            return
        r = fetch(u)
        total = r.getheader("Content-Length")
        total = int(total) if total else None
        first = r.read(CHUNK)
        off = ts_offset(first)
        OFFSET_CACHE[u] = off
        length = (total - off) if total is not None else None
        extra = {"Accept-Ranges": "bytes"}
        if length is None:
            extra["Connection"] = "close"
            self.close_connection = True
        self._headers(200, "video/mp2t", length, extra, cacheable=True)
        buf = bytearray()
        too_big = False
        try:
            if len(first) > off:
                part = first[off:]
                self.wfile.write(part)
                buf += part
            while True:
                data = r.read(CHUNK)
                if not data:
                    break
                self.wfile.write(data)
                if len(buf) <= 4 * 1024 * 1024:
                    buf += data
                else:
                    too_big = True
        except (BrokenPipeError, ConnectionResetError):
            return
        if not too_big and (length is None or len(buf) == length):
            cache_put(u, bytes(buf))

    def _pump(self, r):
        try:
            while True:
                data = r.read(CHUNK)
                if not data:
                    break
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.request_queue_size = 32
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
