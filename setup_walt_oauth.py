"""
One-time OAuth setup untuk Walt Perps MCP.
Jalankan sekali di lokal. Hasil: client_id + refresh_token untuk disimpan
sebagai GitHub Secrets.

Usage:
    python setup_walt_oauth.py

Flow:
    1. Register OAuth client via DCR (RFC 7591)
    2. Generate PKCE challenge
    3. Buka browser untuk authorize Walt
    4. Listen di localhost:8765 untuk callback
    5. Exchange auth_code → refresh_token + access_token
    6. Print hasil untuk disimpan
"""

import base64
import hashlib
import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTH_SERVER = "https://walletbot.me/bot-relay/public-api"
REGISTER_URL = f"{AUTH_SERVER}/oauth/v1/register"
AUTHORIZE_URL = f"{AUTH_SERVER}/oauth/v1/authorize"
TOKEN_URL = f"{AUTH_SERVER}/oauth/v1/token"
REDIRECT_URI = "http://localhost:8765/callback"
SCOPES = "perps.trade perps.readonly"

captured_code = {"value": None, "state": None}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        captured_code["value"] = params.get("code", [None])[0]
        captured_code["state"] = params.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;padding:40px'>"
            b"<h1>Walt OAuth: OK</h1>"
            b"<p>Kembali ke terminal, tokens sedang di-exchange.</p>"
            b"</body></html>"
        )

    def log_message(self, format, *args):
        pass  # suppress noise


def pkce_pair() -> tuple[str, str]:
    """Generate PKCE verifier + S256 challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def register_client() -> dict:
    """Dynamic Client Registration (RFC 7591)."""
    print("1. Register OAuth client via DCR...")
    body = {
        "client_name": "walt-trading-bot",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",  # public client, PKCE-based
        "scope": SCOPES,
    }
    r = httpx.post(REGISTER_URL, json=body, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    print(f"   → client_id: {data['client_id']}")
    return data


def main():
    client = register_client()
    client_id = client["client_id"]

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)

    print("\n2. Buka browser untuk authorize...")
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTHORIZE_URL}?{urlencode(auth_params)}"
    print(f"   URL: {auth_url}")
    webbrowser.open(auth_url)

    print("\n3. Listen di http://localhost:8765/callback...")
    server = HTTPServer(("127.0.0.1", 8765), CallbackHandler)
    server.handle_request()  # satu request lalu stop

    if captured_code["state"] != state:
        raise RuntimeError("state mismatch — potensi CSRF")
    code = captured_code["value"]
    if not code:
        raise RuntimeError("tidak dapat code dari callback")
    print(f"   → auth_code: {code[:20]}...")

    print("\n4. Exchange auth_code → tokens...")
    token_body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    r = httpx.post(TOKEN_URL, data=token_body, timeout=30.0)
    r.raise_for_status()
    tokens = r.json()

    print("\n✅ BERHASIL — simpan sebagai GitHub Secrets:\n")
    print("=" * 70)
    print(f"WALT_CLIENT_ID     = {client_id}")
    print(f"WALT_REFRESH_TOKEN = {tokens['refresh_token']}")
    print("=" * 70)
    print(f"\nAccess token (expire {tokens.get('expires_in', 'N/A')}s, buat test):")
    print(tokens["access_token"])
    print("\nScope diberikan:", tokens.get("scope", SCOPES))

    # Auto-update .env jika file .env ada
    try:
        env_path = ".env"
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                content = f.read()
            import re
            content = re.sub(r"^WALT_CLIENT_ID=.*", f"WALT_CLIENT_ID={client_id}", content, flags=re.M)
            content = re.sub(r"^WALT_REFRESH_TOKEN=.*", f"WALT_REFRESH_TOKEN={tokens['refresh_token']}", content, flags=re.M)
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(content)
            print("\n💾 File .env lokal telah OTOMATIS diperbarui dengan Client ID dan Refresh Token baru.")
    except Exception as err:
        print(f"\n⚠️  Gagal auto-update .env: {err}")

    print("\n⚠️  PENTING: Jangan lupa update KEDUA secret ini di GitHub Settings -> Secrets:")
    print("   1. WALT_CLIENT_ID")
    print("   2. WALT_REFRESH_TOKEN")


if __name__ == "__main__":
    main()
