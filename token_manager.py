"""
Token manager untuk Walt OAuth — auto-refresh access_token.

Dipakai oleh walt_bot.py di GitHub Actions. Butuh env:
    WALT_CLIENT_ID      — dari setup_walt_oauth.py
    WALT_REFRESH_TOKEN  — dari setup_walt_oauth.py
"""

import os
import time
import base64
import threading
import httpx

AUTH_SERVER = "https://walletbot.me/bot-relay/public-api"
TOKEN_URL = f"{AUTH_SERVER}/oauth/v1/token"


def update_github_secret(repo: str, secret_name: str, secret_value: str, pat: str) -> bool:
    """Auto-update repository secret di GitHub menggunakan PyNaCl (libsodium)."""
    try:
        from nacl import encoding, public
    except ImportError:
        print("[WARN] pynacl belum terinstall, skip auto-update GitHub Secret.")
        return False

    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "walt-trading-bot",
    }
    try:
        # 1. Ambil public key repository
        key_url = f"https://api.github.com/repos/{repo}/actions/secrets/public-key"
        r = httpx.get(key_url, headers=headers, timeout=15.0)
        if r.status_code != 200:
            print(f"⚠️  Gagal ambil public key GitHub (HTTP {r.status_code}): {r.text}")
            return False
        key_data = r.json()
        public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder)
        sealed_box = public.SealedBox(public_key)

        # 2. Enkripsi secret dengan public key
        encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
        encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

        # 3. Kirim PUT request untuk update secret
        put_url = f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}"
        payload = {
            "encrypted_value": encrypted_b64,
            "key_id": key_data["key_id"],
        }
        r_put = httpx.put(put_url, headers=headers, json=payload, timeout=15.0)
        if r_put.status_code in (201, 204):
            print(f"✅ [AUTO-SYNC] Berhasil update GitHub Secret '{secret_name}' di {repo}!")
            return True
        else:
            print(f"⚠️  Gagal update secret '{secret_name}' (HTTP {r_put.status_code}): {r_put.text}")
            return False
    except Exception as e:
        print(f"⚠️  Error saat auto-update GitHub Secret: {e}")
        return False


class TokenManager:
    def __init__(self, client_id: str | None = None, refresh_token: str | None = None):
        self.client_id = (client_id or os.environ.get("WALT_CLIENT_ID", "")).strip()
        self.refresh_token = (refresh_token or os.environ.get("WALT_REFRESH_TOKEN", "")).strip()
        if not self.client_id or not self.refresh_token:
            raise ValueError(
                "❌ WALT_CLIENT_ID atau WALT_REFRESH_TOKEN belum di-set atau masih kosong!\n"
                "Silakan tambahkan di Settings -> Secrets and variables -> Actions di GitHub."
            )
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
        try:
            r = httpx.post(TOKEN_URL, data=body, timeout=30.0)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            print(f"❌ Gagal refresh token Walt (HTTP {e.response.status_code}): {e.response.text}")
            raise
        data = r.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        # Rotate refresh_token kalau ada (RFC 6749 §6)
        if "refresh_token" in data and data["refresh_token"] != self.refresh_token:
            self.refresh_token = data["refresh_token"]
            print(f"[WARN] Refresh token di-rotate oleh Walt.")
            print(f"       Token baru: {self.refresh_token[:10]}...{self.refresh_token[-10:]}")

            # 1. Update file .env lokal jika ada
            self._update_local_env(self.refresh_token)

            # 2. Auto-update GitHub Secret jika GH_PAT & GITHUB_REPOSITORY ada
            pat = os.environ.get("GH_PAT")
            repo = os.environ.get("GITHUB_REPOSITORY")
            if pat and repo:
                update_github_secret(repo, "WALT_REFRESH_TOKEN", self.refresh_token, pat)
            else:
                print("[INFO] Simpan token baru ini ke GitHub Secret WALT_REFRESH_TOKEN jika belum pakai GH_PAT.")

    def _update_local_env(self, new_token: str):
        try:
            env_path = ".env"
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    content = f.read()
                import re
                content = re.sub(r"^WALT_REFRESH_TOKEN=.*", f"WALT_REFRESH_TOKEN={new_token}", content, flags=re.M)
                with open(env_path, "w", encoding="utf-8") as f:
                    f.write(content)
                print("💾 File .env lokal telah disinkronkan.")
        except Exception:
            pass


if __name__ == "__main__":
    tm = TokenManager()
    token = tm.get_access_token()
    print(f"access_token (first 30 chars): {token[:30]}...")
    print(f"expires_at: {time.ctime(tm._expires_at)}")
