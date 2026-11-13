"""
Token manager untuk Walt OAuth — auto-refresh access_token.

Dipakai oleh walt_bot.py di GitHub Actions. Butuh env:
    WALT_CLIENT_ID      — dari setup_walt_oauth.py
    WALT_REFRESH_TOKEN  — dari setup_walt_oauth.py
"""

import os
import time
import threading
import httpx

AUTH_SERVER = "https://walletbot.me/bot-relay/public-api"
TOKEN_URL = f"{AUTH_SERVER}/oauth/v1/token"


class TokenManager:
    def __init__(self, client_id: str | None = None, refresh_token: str | None = None):
        self.client_id = client_id or os.environ["WALT_CLIENT_ID"]
        self.refresh_token = refresh_token or os.environ["WALT_REFRESH_TOKEN"]
        self._access_token: str | None = None
        self._expires_at: float = 0
        self._lock = threading.Lock()

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Return fresh access_token. Auto-refresh kalau expired atau mau expired dalam 60s."""
        with self._lock:
            now = time.time()
            if force_refresh or not self._access_token or now > (self._expires_at - 60):
                self._refresh()
            return self._access_token  # type: ignore

    def _refresh(self):
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
        }
        r = httpx.post(TOKEN_URL, data=body, timeout=30.0)
        r.raise_for_status()
        data = r.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        # Rotate refresh_token kalau ada (RFC 6749 §6)
        if "refresh_token" in data and data["refresh_token"] != self.refresh_token:
            self.refresh_token = data["refresh_token"]
            print(f"⚠️  Refresh token di-rotate oleh Walt. Update GitHub Secret WALT_REFRESH_TOKEN:")
            print(f"    {self.refresh_token}")


if __name__ == "__main__":
    tm = TokenManager()
    token = tm.get_access_token()
    print(f"access_token (first 30 chars): {token[:30]}...")
    print(f"expires_at: {time.ctime(tm._expires_at)}")
