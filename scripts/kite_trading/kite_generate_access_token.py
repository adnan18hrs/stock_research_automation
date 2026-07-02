#!/usr/bin/env python3
"""
Generate a Kite Connect access token using a local redirect callback.

Prerequisites:
  1. Kite app Redirect URL:
     http://127.0.0.1:8000/kite/callback
  2. Environment variables:
     KITE_API_KEY
     KITE_API_SECRET

This script does not save secrets. It prints the access token so you can export
it in your VS Code terminal for the trading script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional


KITE_API_BASE = "https://api.kite.trade"
LOGIN_BASE = "https://kite.zerodha.com/connect/login"
KITE_API_HOST = urllib.parse.urlparse(KITE_API_BASE).hostname or "api.kite.trade"


class CallbackResult:
    def __init__(self) -> None:
        self.request_token: Optional[str] = None
        self.status: Optional[str] = None
        self.error: Optional[str] = None


def make_callback_handler(result: CallbackResult, expected_path: str):
    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)

            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Wrong callback path.")
                return

            result.request_token = query.get("request_token", [None])[0]
            result.status = query.get("status", [None])[0]
            result.error = query.get("error", [None])[0] or query.get("message", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Kite token received.</h2>"
                b"<p>You can close this tab and return to VS Code terminal.</p>"
                b"</body></html>"
            )

            threading.Thread(target=self.server.shutdown, daemon=True).start()

    return CallbackHandler


def kite_request_token_login_url(api_key: str, redirect_url: str) -> str:
    params = urllib.parse.urlencode({"v": "3", "api_key": api_key})
    return f"{LOGIN_BASE}?{params}"


def check_kite_api_dns(timeout: int = 5) -> None:
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        socket.getaddrinfo(KITE_API_HOST, 443)
    except socket.gaierror as exc:
        raise RuntimeError(
            f"DNS/network check failed for {KITE_API_HOST}. "
            "Switch network/hotspot, disable blocking VPN/proxy, or fix DNS, then retry."
        ) from exc
    finally:
        socket.setdefaulttimeout(previous_timeout)


def exchange_access_token(api_key: str, api_secret: str, request_token: str, timeout: int) -> Dict[str, Any]:
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode("utf-8")).hexdigest()
    payload = urllib.parse.urlencode(
        {
            "api_key": api_key,
            "request_token": request_token,
            "checksum": checksum,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"{KITE_API_BASE}/session/token",
        data=payload,
        method="POST",
        headers={"X-Kite-Version": "3"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Kite token exchange failed HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Kite token exchange network error: {exc.reason}") from exc

    data = json.loads(body)
    if data.get("status") != "success":
        raise RuntimeError(data.get("message", "Kite token exchange failed"))
    return data["data"]


def wait_for_callback(host: str, port: int, path: str, timeout: int) -> CallbackResult:
    result = CallbackResult()
    server = HTTPServer((host, port), make_callback_handler(result, path))
    server.timeout = 1

    print(f"Listening for Kite redirect on http://{host}:{port}{path}")
    started_at = time.time()
    while time.time() - started_at < timeout and not result.request_token and not result.error:
        server.handle_request()
    server.server_close()

    if not result.request_token:
        raise TimeoutError(f"No request_token received within {timeout} seconds.")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Kite Connect access token.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/kite/callback")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--request-token",
        help="Skip browser login and exchange this request_token directly. Useful after a network/DNS failure.",
    )
    parser.add_argument(
        "--skip-network-check",
        action="store_true",
        help="Skip api.kite.trade DNS preflight check.",
    )
    return parser


def suggested_algo_command() -> str:
    cwd = Path.cwd()
    if cwd.name == "kite_trading":
        return "python3 nifty50_option_algo.py"
    if cwd.name == "scripts":
        return "python3 kite_trading/nifty50_option_algo.py"
    return "python3 scripts/kite_trading/nifty50_option_algo.py"


def main() -> int:
    args = build_parser().parse_args()
    api_key = os.environ.get("KITE_API_KEY")
    api_secret = os.environ.get("KITE_API_SECRET")

    if not api_key or not api_secret:
        raise SystemExit("Set KITE_API_KEY and KITE_API_SECRET in your terminal first.")

    if not args.skip_network_check:
        check_kite_api_dns()
        print(f"Kite API DNS check passed: {KITE_API_HOST}")

    request_token = args.request_token
    if not request_token:
        redirect_url = f"http://{args.host}:{args.port}{args.path}"
        login_url = kite_request_token_login_url(api_key, redirect_url)

        print()
        print("Kite login URL:")
        print(login_url)
        print()
        print("Open this URL in your browser, login to Kite, and approve the app.")
        print(f"Your Kite app Redirect URL must exactly be: {redirect_url}")
        print()

        callback = wait_for_callback(args.host, args.port, args.path, args.timeout)
        if callback.status and callback.status != "success":
            raise RuntimeError(f"Kite callback status={callback.status} error={callback.error}")
        request_token = callback.request_token
        print(f"Request token received: {request_token[:6]}...{request_token[-4:]}")

    try:
        session = exchange_access_token(api_key, api_secret, request_token, args.timeout)
    except RuntimeError as exc:
        print()
        print("Request token was received, but access-token exchange failed.")
        print("Check internet/DNS, then retry quickly with:")
        print(f'python3 kite_generate_access_token.py --request-token "{request_token}"')
        print()
        raise
    access_token = session["access_token"]

    print()
    print("Access token generated. Export it in this same VS Code terminal:")
    print(f'export KITE_ACCESS_TOKEN="{access_token}"')
    print()
    print("Then run interactive mode:")
    print(f"{suggested_algo_command()} --lots 2 --execute --i-understand-live-risk")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
