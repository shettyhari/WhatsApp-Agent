"""EBG WhatsApp Agent - local server.

Run on a PC with:  python server.py   (or double-click start-server.bat)
Serves index.html and the API at http://localhost:8095, storing data in data.json.

The same API runs on Vercel through api/index.py, storing data in Upstash Redis.
All rules live in api/_core.py and api/_agents.py.
"""
import http.server
import json
import os
import socketserver
import sys
import webbrowser
from urllib.parse import parse_qs, urlsplit

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(DIR, "api"))
from _core import Api, FileStorage  # noqa: E402

PORT = int(os.environ.get("AGENT_PORT", "8095"))
PUBLIC_FILES = {"/", "/index.html"}

STORAGE = FileStorage(DIR)
API = Api(STORAGE)


class AgentHandler(http.server.SimpleHTTPRequestHandler):
    timeout = 60

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def path_only(self):
        return self.path.split("?", 1)[0].split("#", 1)[0]

    def send_api(self, method):
        body = b""
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(min(length, 6 * 1024 * 1024)) if length else b""
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items() if v}
        status, payload = API.handle(method, self.path_only(), self.headers, body, self.client_address[0], query=query)
        if isinstance(payload, str):  # plain-text reply (WhatsApp webhook verification)
            raw, ctype = payload.encode("utf-8"), "text/plain; charset=utf-8"
        else:
            raw, ctype = json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path_only()
        if path.startswith("/api/"):
            self.send_api("GET")
            return
        # Only serve the app page. Never expose data.json, sessions.json or server code
        if path not in PUBLIC_FILES:
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self):
        if self.path_only() not in PUBLIC_FILES:
            self.send_error(404)
            return
        super().do_HEAD()

    def do_POST(self):
        if self.path_only().startswith("/api/"):
            self.send_api("POST")
        else:
            self.send_error(404)


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True


def run():
    socketserver.TCPServer.allow_reuse_address = os.name != "nt"
    try:
        httpd = ThreadingServer(("", PORT), AgentHandler)
    except OSError:
        print(f"Port {PORT} is already in use: the WhatsApp Agent server is probably already running.")
        sys.exit(1)
    with httpd:
        print(f"EBG WhatsApp Agent running at http://localhost:{PORT}")
        print("Keep this window open while you use the app. Press Ctrl+C to stop.")
        sys.stdout.flush()
        if "--open" in sys.argv:
            webbrowser.open(f"http://localhost:{PORT}/")
        httpd.serve_forever()


if __name__ == "__main__":
    run()
