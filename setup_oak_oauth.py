"""
One-time OAuth 2.1 setup untuk OAK Research MCP Server (https://oakresearch.io/api/mcp).

Jalankan sekali di lokal untuk mendapatkan `OAK_MCP_CLIENT_ID` dan `OAK_MCP_REFRESH_TOKEN`,
lalu simpan ke `.env` lokal dan GitHub Repository Secrets.

Usage:
    python setup_oak_oauth.py

Flow:
    1. Dynamic Client Registration (RFC 7591) ke https://oakresearch.io/api/mcp/oauth/register
    2. Generate PKCE S256 verifier + challenge
    3. Buka browser untuk sign-in akun OAK Research (akun gratis sudah membuka 9 tool data pasar!)
    4. Tangkap callback di http://localhost:8766/callback
    5. Exchange auth_code -> access_token + refresh_token
    6. Uji langsung `tools/list` ke https://oakresearch.io/api/mcp
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from oak_mcp_client import (
    OAK_AUTHORIZE_URL,
    OAK_MCP_URL,
    OAK_REGISTER_URL,
    OAK_TOKEN_URL,
    OakMCPClient,
)

REDIRECT_URI = "http://localhost:8766/callback"
_captured: dict[str, str | None] = {"code": None, "state": None, "error": None}


class _OakCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        _captured["code"] = params.get("code", [None])[0]
        _captured["state"] = params.get("state", [None])[0]
        _captured["error"] = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;padding:40px'>"
            b"<h1>OAK Research MCP OAuth: OK</h1>"
            b"<p>Kembali ke terminal, token sedang di-exchange dan diverifikasi.</p>"
            b"</body></html>"
        )

    def log_message(self, format, *args):
        pass


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def register_oak_client() -> dict:
    print("1. Registering OAuth 2.1 public client via DCR di OAK Research...")
    body = {
        "client_name": "walt-trading-research-bot",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    r = httpx.post(OAK_REGISTER_URL, json=body, timeout=25.0)
    r.raise_for_status()
    data = r.json()
    print(f"   -> client_id: {data['client_id']}")
    return data


def main() -> None:
    reg = register_oak_client()
    client_id = reg["client_id"]
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": OAK_MCP_URL,
    }
    auth_url = f"{OAK_AUTHORIZE_URL}?{urlencode(auth_params)}"
    print("\n2. Membuka browser untuk login akun OAK Research...")
    print(f"   URL: {auth_url}")
    webbrowser.open(auth_url)

    print(f"\n3. Menunggu callback di {REDIRECT_URI} ...")
    server = HTTPServer(("127.0.0.1", 8766), _OakCallbackHandler)
    server.handle_request()

    if _captured["error"]:
        raise RuntimeError(f"OAuth error dari OAK Research: {_captured['error']}")
    if _captured["state"] != state:
        raise RuntimeError("State mismatch — dibatalkan demi keamanan CSRF.")
    code = _captured["code"]
    if not code:
        raise RuntimeError("Authorization code tidak ditemukan di callback.")

    print("4. Menukar authorization_code menjadi token...")
    tok_resp = httpx.post(
        OAK_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "resource": OAK_MCP_URL,
        },
        headers={"Accept": "application/json"},
        timeout=25.0,
    )
    tok_resp.raise_for_status()
    tokens = tok_resp.json()
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    print("5. Memverifikasi koneksi langsung ke OAK MCP Server (`tools/list`)...")
    cli = OakMCPClient(
        access_token=access_token,
        client_id=client_id,
        refresh_token=refresh_token,
    )
    tools = cli.list_tools()
    tool_names = [t.get("name", "") for t in tools]
    print(f"   -> BERHASIL terhubung! {len(tool_names)} tools tersedia: {', '.join(tool_names)}")

    print("\n=== SIMPAN KE .env LOKAL & GITHUB SECRETS ===")
    print(f"OAK_MCP_CLIENT_ID={client_id}")
    if refresh_token:
        print(f"OAK_MCP_REFRESH_TOKEN={refresh_token}")
    else:
        print(f"OAK_MCP_ACCESS_TOKEN={access_token}")
    print("OAK_MCP_ENABLED=on")


if __name__ == "__main__":
    main()
