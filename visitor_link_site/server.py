#!/usr/bin/env python3
"""
Consent-based product link site with instant email alerts.

Run:
  python3 server.py
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
DATA_DIR = BASE_DIR / "data"
EVENT_LOG = DATA_DIR / "events.jsonl"


def load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def first_header(headers: Any, name: str) -> str:
    value = headers.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def client_ip(handler: SimpleHTTPRequestHandler) -> str:
    candidates = [
        first_header(handler.headers, "CF-Connecting-IP"),
        first_header(handler.headers, "X-Real-IP"),
        first_header(handler.headers, "X-Forwarded-For").split(",")[0].strip(),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return handler.client_address[0] if handler.client_address else ""


def public_ip_for_lookup(ip_address: str) -> bool:
    if not ip_address:
        return False
    private_prefixes = ("127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.")
    return not ip_address.startswith(private_prefixes) and ip_address not in {"::1", "localhost"}


def lookup_ip(ip_address: str) -> dict[str, Any]:
    token = env("IPINFO_TOKEN")
    if not token or not public_ip_for_lookup(ip_address):
        return {"enabled": bool(token), "available": False}

    url = f"https://ipinfo.io/{ip_address}/json?token={token}"
    request = urllib.request.Request(url, headers={"User-Agent": "visitor-link-site/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 200:
                return {"enabled": True, "available": False, "status": response.status}
            return {"enabled": True, "available": True, "data": json.loads(response.read().decode("utf-8"))}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"enabled": True, "available": False, "error": str(exc)}


def flatten(value: Any, fallback: str = "-") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, indent=2)
    return str(value)


def email_body(event: dict[str, Any]) -> str:
    client = event.get("client", {})
    server = event.get("server", {})
    geo = event.get("ip_geo", {})
    precise_geo = client.get("precise_location")

    lines = [
        "New consented visitor event",
        "",
        f"Event: {event.get('event_type', '-')}",
        f"Time UTC: {event.get('received_at', '-')}",
        f"Product: {client.get('product_title', '-')}",
        f"Destination: {client.get('destination_url', '-')}",
        f"Visitor shared email: {client.get('shared_email', '-')}",
        f"Identity source: {client.get('identity_source', '-')}",
        "",
        "Server details",
        f"IP: {server.get('ip', '-')}",
        f"User-Agent: {server.get('user_agent', '-')}",
        f"Accept-Language: {server.get('accept_language', '-')}",
        f"Referrer header: {server.get('referer', '-')}",
        "",
        "Browser details",
        f"Visitor ID: {client.get('visitor_id', '-')}",
        f"Session ID: {client.get('session_id', '-')}",
        f"Page URL: {client.get('page_url', '-')}",
        f"Document referrer: {client.get('document_referrer', '-')}",
        f"UTM: {flatten(client.get('utm'))}",
        f"Language: {client.get('language', '-')}",
        f"Languages: {flatten(client.get('languages'))}",
        f"Timezone: {client.get('timezone', '-')}",
        f"Platform: {client.get('platform', '-')}",
        f"Screen: {flatten(client.get('screen'))}",
        f"Viewport: {flatten(client.get('viewport'))}",
        f"Device memory: {client.get('device_memory', '-')}",
        f"CPU cores: {client.get('hardware_concurrency', '-')}",
        f"Cookies enabled: {client.get('cookie_enabled', '-')}",
        f"Do Not Track: {client.get('do_not_track', '-')}",
        "",
        "Approximate IP location",
        flatten(geo),
        "",
        "Precise browser location",
        flatten(precise_geo),
        "",
        "Note: Instagram username, name, phone, and email are not available unless the visitor submits them or logs in with permission.",
    ]
    return "\n".join(lines)


def send_email(event: dict[str, Any]) -> dict[str, Any]:
    smtp_host = env("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(env("SMTP_PORT", "587"))
    smtp_username = env("SMTP_USERNAME")
    smtp_password = env("SMTP_PASSWORD")
    alert_to = env("ALERT_TO_EMAIL")
    from_email = env("FROM_EMAIL", smtp_username)

    if not all([smtp_host, smtp_username, smtp_password, alert_to, from_email]):
        return {"sent": False, "reason": "SMTP env vars not configured"}

    event_type = event.get("event_type", "visitor_event")
    ip_address = event.get("server", {}).get("ip", "-")
    message = EmailMessage()
    message["Subject"] = f"Visitor alert: {event_type} from {ip_address}"
    message["From"] = from_email
    message["To"] = alert_to
    message.set_content(email_body(event))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
        smtp.starttls(context=context)
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)

    return {"sent": True}


def save_event(event: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{now_iso()} {self.address_string()} {format % args}")

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/track":
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return

        content_length = int(first_header(self.headers, "Content-Length") or "0")
        if content_length > 64_000:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "Payload too large"})
            return

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid JSON"})
            return

        if not payload.get("consent"):
            self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Consent required"})
            return

        ip_address = client_ip(self)
        event = {
            "received_at": now_iso(),
            "event_type": payload.get("event_type", "unknown"),
            "client": payload.get("client", {}),
            "server": {
                "ip": ip_address,
                "user_agent": first_header(self.headers, "User-Agent"),
                "accept_language": first_header(self.headers, "Accept-Language"),
                "referer": first_header(self.headers, "Referer"),
                "origin": first_header(self.headers, "Origin"),
            },
            "ip_geo": lookup_ip(ip_address),
        }

        try:
            email_status = send_email(event)
        except Exception as exc:  # Email failure should not break the visitor flow.
            email_status = {"sent": False, "error": str(exc)}

        event["email"] = email_status
        save_event(event)
        self.send_json(HTTPStatus.OK, {"ok": True, "email": email_status})


def main() -> None:
    host = env("HOST", "127.0.0.1")
    port = int(env("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Visitor link site running at http://{host}:{port}")
    print(f"Events log: {EVENT_LOG}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
