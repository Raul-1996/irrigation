#!/usr/bin/env python3
"""Threaded reverse proxy with HTTP Basic Auth.

Used as a lightweight auth layer in front of the irrigation app,
typically behind a Cloudflare Tunnel.

Configuration via environment variables:
    AUTH_USER       - username (default: admin)
    AUTH_PASS       - password (default: admin)
    AUTH_REALM      - auth realm shown in browser popup (default: Irrigation)
    UPSTREAM_PORT   - upstream app port (default: 8080)
    LISTEN_PORT     - proxy listen port (default: 8081)
"""
import http.server
import socketserver
import urllib.request
import base64
import os

UPSTREAM = "http://127.0.0.1:" + os.environ.get("UPSTREAM_PORT", "8080")
USER = os.environ.get("AUTH_USER", "admin")
PASS = os.environ.get("AUTH_PASS", "admin")
REALM = os.environ.get("AUTH_REALM", "Irrigation")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8081"))

HOP_BY_HOP = frozenset(("transfer-encoding", "connection", "keep-alive", "te", "trailers", "upgrade"))


class AuthProxy(http.server.BaseHTTPRequestHandler):
    def _check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            u, p = decoded.split(":", 1)
            return u == USER and p == PASS
        except Exception:
            return False

    def _proxy(self):
        if not self._check_auth():
            self.send_response(401)
            self.send_header("WWW-Authenticate", f"Basic realm=\"{REALM}\"")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None

        url = UPSTREAM + self.path
        headers = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "authorization") and k.lower() not in HOP_BY_HOP:
                headers[k] = v
        headers["Host"] = "127.0.0.1:" + os.environ.get("UPSTREAM_PORT", "8080")

        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            self.end_headers()
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, format, *args):
        pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _proxy


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    server = ThreadedServer(("", LISTEN_PORT), AuthProxy)
    print(f"Auth proxy :{LISTEN_PORT} -> {UPSTREAM} (user={USER})")
    server.serve_forever()
