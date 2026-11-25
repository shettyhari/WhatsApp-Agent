"""Vercel serverless entry point for the EBG WhatsApp Agent API.

vercel.json rewrites every /api/<route> request here as /api/index?route=<route>.
Data is stored in Upstash Redis (env: UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN,
or the KV_REST_API_URL / KV_REST_API_TOKEN names Vercel's integration also sets).
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _core import Api, storage_from_env  # noqa: E402


def resolve_path(raw_path):
    parts = urlsplit(raw_path)
    route = parse_qs(parts.query).get("route", [""])[0]
    if route:
        return "/api/" + route.strip("/")
    return parts.path.rstrip("/") or "/"


def query_params(raw_path):
    """Query string values other than the rewrite's own `route` (e.g. Meta's hub.* webhook check)."""
    params = parse_qs(urlsplit(raw_path).query)
    return {k: v[0] for k, v in params.items() if k != "route" and v}


def client_ip(handler):
    forwarded = handler.headers.get("x-forwarded-for") or handler.headers.get("x-real-ip") or ""
    return forwarded.split(",")[0].strip() or (handler.client_address[0] if handler.client_address else "")


PAGE_PATHS = {"/", "/index.html"}
HERE = os.path.dirname(os.path.abspath(__file__))


def read_page():
    """The app page, for when Vercel sends page requests to this function too."""
    for path in (os.path.join(HERE, "..", "index.html"), os.path.join(os.getcwd(), "index.html")):
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            continue
    return None


class handler(BaseHTTPRequestHandler):  # Vercel looks for a class named `handler`
    def _send(self, status, raw, ctype, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _respond(self, method):
        path = resolve_path(self.path)
        # Normally Vercel serves index.html itself. Some project settings send every request
        # here instead, so serve the page from the function as well.
        if method == "GET" and not path.startswith("/api/"):
            page = read_page() if path in PAGE_PATHS else None
            if page is not None:
                self._send(200, page, "text/html; charset=utf-8", cache="no-cache")
            else:
                self._send(404, f"Not found: {path}".encode("utf-8"), "text/plain; charset=utf-8")
            return
        body = b""
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(min(length, 6 * 1024 * 1024)) if length else b""
        api = Api(storage_from_env())
        status, payload = api.handle(method, path, self.headers, body, client_ip(self),
                                     query=query_params(self.path))
        if isinstance(payload, str):  # plain-text reply (WhatsApp webhook verification)
            self._send(status, payload.encode("utf-8"), "text/plain; charset=utf-8")
        else:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        self._respond("GET")

    def do_POST(self):
        self._respond("POST")
