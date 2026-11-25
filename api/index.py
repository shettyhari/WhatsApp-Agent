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


class handler(BaseHTTPRequestHandler):  # Vercel looks for a class named `handler`
    def _respond(self, method):
        body = b""
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(min(length, 6 * 1024 * 1024)) if length else b""
        api = Api(storage_from_env())
        status, payload = api.handle(method, resolve_path(self.path), self.headers, body, client_ip(self),
                                     query=query_params(self.path))
        if isinstance(payload, str):  # plain-text reply (WhatsApp webhook verification)
            raw, ctype = payload.encode("utf-8"), "text/plain; charset=utf-8"
        else:
            raw, ctype = json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._respond("GET")

    def do_POST(self):
        self._respond("POST")
