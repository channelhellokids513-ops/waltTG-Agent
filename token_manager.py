"""
Token manager untuk Walt OAuth — auto-refresh access_token.

Dipakai oleh walt_bot.py di GitHub Actions. Butuh env:
    WALT_CLIENT_ID      — dari setup_walt_oauth.py
    WALT_REFRESH_TOKEN  — dari setup_walt_oauth.py
"""

import os
import re
import time
import base64
import logging
import threading
from pathlib import Path
import httpx

from notifier import md_escape


def require_persistable_refresh() -> None:
    """Fail-closed SEBELUM satu byte pun dikirim ke Walt (#44926211013, 16:44:44Z).

    Refresh token Walt bersifat single-use: setiap POST /oauth/v1/token yang sukses membakar
    token lama. Kalau proses ini tidak punya tempat menyimpan token baru, kredensial CI mati
    untuk SEMUA run berikutnya. Ronde ini membuktikannya dua arah:
      - WALT_TOKEN_SYNC=off  -> allow_refresh() sudah menahan (R6-fix);
      - WALT_TOKEN_SYNC=on TAPI GH_PAT/GITHUB_REPOSITORY tidak dioper ke langkah itu ->
        token TETAP terbakar dan alert-nya keluar sesudah kerusakan terjadi.
    Aturan baru: izin refresh mensyaratkan jalur persistensi yang benar-benar ada.
    """
    allow_refresh()  # pertahankan gerbang lama (sync off => tolak)
    pat = (os.environ.get("GH_PAT") or "").strip()
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if pat and repo:
        return
    in_ci = os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
    has_local_env = (Path(__file__).resolve().parent / ".env").exists()
    if not in_ci and has_local_env:
        return  # jalur lokal dengan .env: token baru tertulis ke .env, aman
    why = "berjalan di GitHub Actions" if in_ci else "tidak ada .env lokal"
    missing = "GH_PAT" if not pat else "GITHUB_REPOSITORY"
    raise RuntimeError(
        f"Menolak refresh OAuth SEBELUM memanggil Walt: {missing} kosong padahal {why} dan "
        "persistensi token baru diizinkan. Refresh akan membakar refresh token single-use "
        "tanpa tempat menyimpan gantinya. Operkan GH_PAT + GITHUB_REPOSITORY di env langkah "
        "yang sama, atau set WALT_TOKEN_SYNC=off untuk langkah read-only."
    )

log = logging.getLogger("token_manager")

AUTH_SERVER = "https://walletbot.me/bot-relay/public-api"
TOKEN_URL = f"{AUTH_SERVER}/oauth/v1/token"

# R6-4: workflow berlabel read-only tidak boleh mengonsumsi rotasi refresh token single-use.
# Default "on" = perilaku jalur trading tidak berubah sama sekali.
TOKEN_SYNC_ENV = "WALT_TOKEN_SYNC"
_TOKEN_SYNC_OFF = {"off", "0", "false", "no", "read-only", "readonly"}


def token_sync_enabled() -> bool:
    """False hanya bila WALT_TOKEN_SYNC diset eksplisit ke nilai mati (off/false/0/no)."""
    return (os.environ.get(TOKEN_SYNC_ENV, "on").strip().lower() not in _TOKEN_SYNC_OFF)


def allow_refresh() -> None:
    """
    Invarian yang ditegakkan di kode (diduga salah di ronde ini, dibuktikan run nyata):
    setiap refresh OAuth MEMBAKAR refresh token single-use dan menghasilkan token baru. Bila
    proses ini tidak bisa menyimpan token baru (mode read-only), hasil refresh akan hilang dan
    kredensial di secret/`.env` menjadi mati — untuk semua run berikutnya. Maka: tidak bisa
    persist => tidak boleh refresh.
    """
    if not token_sync_enabled():
        raise RuntimeError(
            f"{TOKEN_SYNC_ENV}=off: refresh OAuth akan mengonsumsi refresh token single-use tanpa "
            "menyimpan hasilnya, yang membuat kredensial mati. Jalankan dengan "
            f"{TOKEN_SYNC_ENV}=on (tulis-balik secret) atau hindungi panggilan yang butuh token."
        )


def _redact_oauth_snippet(text: str, limit: int = 200) -> str:
    """Redaksi nilai token pada potongan respons OAuth dan potong maks `limit` karakter."""
    cleaned = re.sub(
        r'(?i)("(?:access_token|refresh_token|id_token|token|secret)"\s*:\s*")([^"]+)(")',
        r"\1***\3",
        str(text or ""),
    )
    cleaned = re.sub(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{8,})", r"\1***", cleaned)
    return cleaned[:limit]


def _notify_telegram_safe(msg: str) -> None:
    try:
        import notifier
        notifier.send_telegram(msg)
    except Exception as err:
        log.warning(f"token_manager._notify_telegram_safe gagal ({type(err).__name__}): {err}")


def update_github_secret(repo: str, secret_name: str, secret_value: str, pat: str) -> bool:
    """Auto-update repository secret di GitHub menggunakan PyNaCl (libsodium)."""
    try:
        from nacl import encoding, public
    except ImportError as e:
        log.warning(f"update_github_secret: pynacl tidak terpasang ({type(e).__name__}): {e}")
        _notify_telegram_safe("⚠️ pynacl tidak terpasang — auto-sync GitHub Secret mati")
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
        # Invarian R6-fix + R8: tidak ada refresh tanpa persistensi NYATA (token single-use).
        # Guard ini DIEKSEKUSI SEBELUM httpx.post - kegagalan terjadi di sisi kita, bukan
        # setelah token di server Walt terbakar.
        require_persistable_refresh()
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
        }
        try:
            r = httpx.post(TOKEN_URL, data=body, timeout=30.0)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            snippet = _redact_oauth_snippet(e.response.text, limit=200)
            log.error(f"TokenManager._refresh HTTP {e.response.status_code}: {snippet}")
            if e.response.status_code == 400 and "invalid_grant" in (e.response.text or "").lower():
                _notify_telegram_safe(
                    f"⚠️ *ALERT OAUTH {md_escape('invalid_grant')}*: refresh token Walt ditolak "
                    "(HTTP 400). "
                    "Ada DUA kemungkinan dengan tindakan berbeda:\n"
                    "1) *Ras antar-konsumen* (paling sering): workflow lain merotasi token "
                    "single-use tepat sebelum/saat workflow ini start — bisa CI vs CI (mis. "
                    "risk-monitor + dispatch manual) atau CI vs lokal. Pembeda: "
                    "`gh run list --limit 6`. Bila ada run OAuth *sukses* dalam ~15 menit terakhir, "
                    "ini BUKAN kematian kredensial; siklus berikutnya pulih sendiri, JANGAN re-auth.\n"
                    "2) *Kredensial benar-benar habis*: semua run OAuth merah beruntun. Jalankan "
                    "runbook README *Jendela Pemeliharaan & Pemulihan Kredensial* "
                    "(MAINTENANCE_MODE=true → python setup_walt_oauth.py → perbarui 2 secret)."
                )
            raise
        try:
            data = r.json()
        except Exception as e:
            snippet = _redact_oauth_snippet(getattr(r, "text", ""), limit=200)
            raise RuntimeError(
                f"Respons OAuth bukan JSON valid ({type(e).__name__}): {snippet}"
            ) from e
        if not isinstance(data, dict):
            snippet = _redact_oauth_snippet(getattr(r, "text", ""), limit=200)
            raise RuntimeError(f"Respons OAuth bukan objek JSON: {snippet}")
        access = data.get("access_token")
        if not access:
            raise RuntimeError(f"Respons OAuth tanpa access_token: keys={list(data)}")
        self._access_token = access
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        # Rotate refresh_token kalau ada (RFC 6749 §6)
        if "refresh_token" in data and data["refresh_token"] != self.refresh_token:
            self.refresh_token = data["refresh_token"]
            log.warning("TokenManager._refresh: refresh_token di-rotate oleh Walt.")

            # Token lama sudah terbakar; satu-satunya jalan selamat adalah menyimpan token baru
            # (secret GitHub dan/atau .env lokal). allow_refresh() menjamin kita di sini hanya
            # bila persistensi diizinkan.
            # 1. Update file .env lokal jika ada
            self._update_local_env(self.refresh_token)

            # 2. Auto-update GitHub Secret jika GH_PAT & GITHUB_REPOSITORY ada
            pat = os.environ.get("GH_PAT")
            repo = os.environ.get("GITHUB_REPOSITORY")
            synced = False
            if pat and repo:
                synced = update_github_secret(repo, "WALT_REFRESH_TOKEN", self.refresh_token, pat)
            else:
                if not pat:
                    log.warning("[AUTO-SYNC GAGAL] Secret 'GH_PAT' belum ada di GitHub Secrets.")
                if not repo:
                    log.warning("[AUTO-SYNC GAGAL] GITHUB_REPOSITORY tidak terdeteksi.")

            if not synced:
                _notify_telegram_safe(
                    "⚠️ *ALERT TOKEN ROTATION*: `WALT_REFRESH_TOKEN` telah di-rotate oleh Walt, "
                    "tetapi auto-update GitHub Secret gagal atau `GH_PAT` tidak tersedia. "
                    "Harap perbarui secret `WALT_REFRESH_TOKEN` secara manual!"
                )

    def _update_local_env(self, new_token: str):
        env_path = Path(__file__).resolve().parent / ".env"
        if os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true" and not env_path.exists():
            log.info(
                "TokenManager._update_local_env: berjalan di GitHub Actions tanpa .env lokal (normal — sinkronisasi via GitHub Secret)."
            )
            return
        try:
            if not env_path.exists():
                log.warning(
                    f"TokenManager._update_local_env: file .env tidak ditemukan di {env_path}"
                )
                _notify_telegram_safe(
                    "⚠️ *TOKEN ROTATION*: file `.env` lokal tidak ditemukan sehingga "
                    "`WALT_REFRESH_TOKEN` lokal tidak tersinkron."
                )
                return
            content = env_path.read_text(encoding="utf-8")
            new_content, count = re.subn(
                r"^WALT_REFRESH_TOKEN=.*",
                lambda _m: f"WALT_REFRESH_TOKEN={new_token}",
                content,
                flags=re.M,
            )
            if count == 0:
                log.warning(
                    f"TokenManager._update_local_env: kunci WALT_REFRESH_TOKEN tidak ditemukan di {env_path}"
                )
                _notify_telegram_safe(
                    "⚠️ *TOKEN ROTATION*: kunci `WALT_REFRESH_TOKEN` tidak ditemukan di `.env` lokal "
                    "sehingga file lokal tidak tersinkron."
                )
                return
            env_path.write_text(new_content, encoding="utf-8")
            log.info("TokenManager._update_local_env: file .env lokal telah disinkronkan.")
        except OSError as e:
            log.warning(
                f"TokenManager._update_local_env gagal I/O ({type(e).__name__}): {e}"
            )
            _notify_telegram_safe(
                "⚠️ *TOKEN ROTATION*: gagal menulis `.env` lokal sehingga `WALT_REFRESH_TOKEN` lokal tidak tersinkron."
            )


if __name__ == "__main__":
    tm = TokenManager()
    tm.get_access_token()
    print("access_token berhasil diperoleh.")
    print(f"expires_at: {time.ctime(tm._expires_at)}")
