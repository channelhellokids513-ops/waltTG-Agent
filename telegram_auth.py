"""
Sistem keamanan & RBAC (Role-Based Access Control) + Google OAuth 2.0 Device Flow
untuk Bot Telegram Walt Trading.

Roles:
    - admin    : Kontrol penuh (kelola user/role, ubah strategi, eksekusi/close posisi, lihat keuangan & chart)
    - planner  : Financial Advisor (lihat keuangan & chart, ubah parameter strategi; tidak bisa kelola user/eksekusi manual)
    - end_user : Read-only / konfirmasi (lihat status keuangan, posisi, histori, chart, dan strategi aktif)
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

Role = Literal["admin", "planner", "end_user"]

ROLE_PERMISSIONS: dict[Role, set[str]] = {
    "admin": {
        "view_finance",
        "view_chart",
        "view_strategy",
        "edit_strategy",
        "manage_users",
        "trade_control",
        "view_audit",
    },
    "planner": {
        "view_finance",
        "view_chart",
        "view_strategy",
        "edit_strategy",
    },
    "end_user": {
        "view_finance",
        "view_chart",
        "view_strategy",
    },
}

ROLE_LABELS: dict[Role, str] = {
    "admin": "Administrator",
    "planner": "Planner (Financial Advisor)",
    "end_user": "End User",
}

DEFAULT_AUTH_FILE = Path("logs") / "telegram_auth.json"
DEFAULT_AUDIT_FILE = Path("logs") / "security_audit.jsonl"

GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _now_ts() -> int:
    return int(time.time())


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_ttl_seconds() -> int:
    hours_raw = (os.environ.get("AUTH_SESSION_TTL_HOURS") or "24").strip()
    try:
        hours = max(1.0, float(hours_raw))
    except ValueError:
        hours = 24.0
    return int(hours * 3600)


def is_google_oauth_configured() -> bool:
    cid = (os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    csec = (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
    return bool(cid and csec and cid.lower() != "dummy" and csec.lower() != "dummy")


def require_google_auth_strict() -> bool:
    return (os.environ.get("REQUIRE_GOOGLE_AUTH") or "false").strip().lower() in {"1", "true", "yes", "on"}


class AuthManager:
    """Mengelola autentikasi Google OAuth, whitelist user Telegram, sesi, dan audit log."""

    def __init__(
        self,
        auth_file: Path = DEFAULT_AUTH_FILE,
        audit_file: Path = DEFAULT_AUDIT_FILE,
    ):
        self.auth_file = Path(auth_file)
        self.audit_file = Path(audit_file)
        self._lock = threading.Lock()
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        self.audit_file.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_store()

    def _load(self) -> dict[str, Any]:
        if not self.auth_file.exists():
            return {"users": [], "pending_flows": {}}
        try:
            data = json.loads(self.auth_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("users", [])
                data.setdefault("pending_flows", {})
                return data
        except Exception:
            pass
        return {"users": [], "pending_flows": {}}

    def _save(self, data: dict[str, Any]) -> None:
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        self.auth_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _ensure_store(self) -> None:
        with self._lock:
            data = self._load()
            changed = False
            users: list[dict[str, Any]] = data["users"]

            owner_chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
            admin_email = (os.environ.get("ADMIN_GOOGLE_EMAIL") or "").strip().lower()

            has_admin = any(u.get("role") == "admin" and u.get("active", True) for u in users)
            if not has_admin and owner_chat_id and owner_chat_id.lower() != "dummy":
                try:
                    tg_id = int(owner_chat_id)
                except ValueError:
                    tg_id = None
                if tg_id is not None:
                    existing = next((u for u in users if u.get("telegram_id") == tg_id), None)
                    if existing is None:
                        users.append(
                            {
                                "google_email": admin_email or f"owner-{tg_id}@local",
                                "role": "admin",
                                "telegram_id": tg_id,
                                "chat_id": tg_id,
                                "active": True,
                                "auth_method": "bootstrap_owner",
                                "authenticated_at": _now_ts(),
                                "session_expires_at": _now_ts() + 365 * 86400,
                                "created_at": _iso_now(),
                            }
                        )
                        changed = True
            if admin_email:
                by_email = next(
                    (u for u in users if (u.get("google_email") or "").lower() == admin_email),
                    None,
                )
                if by_email is None:
                    users.append(
                        {
                            "google_email": admin_email,
                            "role": "admin",
                            "telegram_id": None,
                            "chat_id": None,
                            "active": True,
                            "auth_method": "google_oauth",
                            "authenticated_at": 0,
                            "session_expires_at": 0,
                            "created_at": _iso_now(),
                        }
                    )
                    changed = True

            if changed or not self.auth_file.exists():
                self._save(data)

    def log_audit(
        self,
        event: str,
        telegram_id: int | None,
        username: str = "",
        detail: str = "",
        role: str = "",
    ) -> None:
        entry = {
            "ts": _iso_now(),
            "event": event,
            "telegram_id": telegram_id,
            "username": username,
            "role": role,
            "detail": detail,
        }
        with self._lock:
            self.audit_file.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent_audit(self, limit: int = 15) -> list[dict[str, Any]]:
        if not self.audit_file.exists():
            return []
        lines = self.audit_file.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return list(reversed(out))

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._load()["users"])

    def get_user_by_telegram(self, telegram_id: int) -> dict[str, Any] | None:
        with self._lock:
            data = self._load()
            for u in data["users"]:
                if u.get("telegram_id") == telegram_id and u.get("active", True):
                    return dict(u)
        return None

    def authorize(
        self,
        telegram_id: int,
        permission: str | None = None,
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """
        Memeriksa apakah telegram_id terautentikasi dan memiliki permission tertentu.
        Return: (allowed, user_record, reason)
        """
        user = self.get_user_by_telegram(telegram_id)
        if not user:
            return False, None, "Akun Telegram Anda belum terdaftar / terverifikasi. Gunakan /login dengan Google Auth."

        auth_method = user.get("auth_method", "google_oauth")
        if require_google_auth_strict() and auth_method != "google_oauth":
            return False, user, "Mode Google Auth ketat aktif. Silakan login ulang dengan /login."

        expires_at = int(user.get("session_expires_at") or 0)
        if expires_at and _now_ts() > expires_at:
            return False, user, "Sesi login Anda sudah kedaluwarsa. Silakan /login ulang."

        role = user.get("role", "end_user")
        if role not in ROLE_PERMISSIONS:
            return False, user, f"Role '{role}' tidak valid."

        if permission and permission not in ROLE_PERMISSIONS[role]:
            return (
                False,
                user,
                f"Akses ditolak 🔒. Role Anda ({ROLE_LABELS.get(role, role)}) tidak memiliki izin '{permission}'.",
            )
        return True, user, "OK"

    def add_or_update_user(
        self,
        google_email: str,
        role: Role,
        telegram_id: int | None = None,
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValueError(f"Role tidak valid: {role}. Pilih: admin, planner, end_user")
        email = google_email.strip().lower()
        if "@" not in email:
            raise ValueError("Format email Google tidak valid.")

        with self._lock:
            data = self._load()
            users = data["users"]
            existing = next(
                (u for u in users if (u.get("google_email") or "").lower() == email),
                None,
            )
            if existing is None and telegram_id is not None:
                existing = next((u for u in users if u.get("telegram_id") == telegram_id), None)

            if existing is not None:
                existing["google_email"] = email
                existing["role"] = role
                existing["active"] = True
                if telegram_id is not None:
                    existing["telegram_id"] = telegram_id
                    existing["chat_id"] = telegram_id
                    if existing.get("auth_method") != "google_oauth":
                        existing["auth_method"] = "admin_whitelist"
                        existing["authenticated_at"] = _now_ts()
                        existing["session_expires_at"] = _now_ts() + _session_ttl_seconds()
                record = dict(existing)
            else:
                record = {
                    "google_email": email,
                    "role": role,
                    "telegram_id": telegram_id,
                    "chat_id": telegram_id,
                    "active": True,
                    "auth_method": "admin_whitelist" if telegram_id is not None else "google_oauth",
                    "authenticated_at": _now_ts() if telegram_id is not None else 0,
                    "session_expires_at": (_now_ts() + _session_ttl_seconds()) if telegram_id is not None else 0,
                    "created_at": _iso_now(),
                }
                users.append(record)
            self._save(data)
            return record

    def set_role(self, identifier: str, new_role: Role) -> dict[str, Any] | None:
        if new_role not in ROLE_PERMISSIONS:
            raise ValueError(f"Role tidak valid: {new_role}")
        ident = identifier.strip().lower()
        with self._lock:
            data = self._load()
            for u in data["users"]:
                if (u.get("google_email") or "").lower() == ident or str(u.get("telegram_id")) == ident:
                    u["role"] = new_role
                    self._save(data)
                    return dict(u)
        return None

    def revoke_user(self, identifier: str) -> dict[str, Any] | None:
        ident = identifier.strip().lower()
        with self._lock:
            data = self._load()
            for u in data["users"]:
                if (u.get("google_email") or "").lower() == ident or str(u.get("telegram_id")) == ident:
                    u["active"] = False
                    u["session_expires_at"] = 0
                    self._save(data)
                    return dict(u)
        return None

    def logout(self, telegram_id: int) -> bool:
        with self._lock:
            data = self._load()
            for u in data["users"]:
                if u.get("telegram_id") == telegram_id and u.get("active", True):
                    u["session_expires_at"] = 0
                    if u.get("auth_method") == "google_oauth":
                        u["telegram_id"] = None
                        u["chat_id"] = None
                    self._save(data)
                    return True
        return False

    # -------------------------------------------------------------------------
    # GOOGLE OAUTH 2.0 DEVICE AUTHORIZATION FLOW
    # -------------------------------------------------------------------------

    def start_google_device_login(self, telegram_id: int, chat_id: int) -> dict[str, Any]:
        """Memulai Google OAuth 2.0 Device Flow dan mengembalikan verification_url & user_code."""
        if not is_google_oauth_configured():
            raise RuntimeError(
                "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET belum dikonfigurasi."
            )
        client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"].strip()
        r = httpx.post(
            GOOGLE_DEVICE_CODE_URL,
            data={
                "client_id": client_id,
                "scope": "openid email profile",
            },
            timeout=15.0,
        )
        r.raise_for_status()
        payload = r.json()
        flow_info = {
            "device_code": payload["device_code"],
            "user_code": payload["user_code"],
            "verification_url": payload.get("verification_url") or payload.get("verification_uri", "https://www.google.com/device"),
            "expires_in": int(payload.get("expires_in", 900)),
            "interval": int(payload.get("interval", 5)),
            "telegram_id": telegram_id,
            "chat_id": chat_id,
            "created_at": _now_ts(),
        }
        with self._lock:
            data = self._load()
            data["pending_flows"][str(telegram_id)] = flow_info
            self._save(data)
        return flow_info

    def poll_google_device_login(self, telegram_id: int) -> tuple[str, dict[str, Any] | None, str]:
        """
        Poll token endpoint sekali.
        Return status: ("pending" | "success" | "denied" | "expired" | "error", user_record, message)
        """
        with self._lock:
            data = self._load()
            flow = data["pending_flows"].get(str(telegram_id))

        if not flow:
            return "error", None, "Tidak ada sesi login Google yang tertunda."

        if _now_ts() - int(flow["created_at"]) > int(flow["expires_in"]):
            self._clear_pending_flow(telegram_id)
            return "expired", None, "Kode login Google sudah kedaluwarsa. Jalankan /login lagi."

        client_id = (os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
        client_secret = (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()

        r = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": flow["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=15.0,
        )
        tok = r.json()
        if r.status_code != 200:
            err = tok.get("error", "")
            if err in {"authorization_pending", "slow_down"}:
                return "pending", None, "Menunggu konfirmasi login Google..."
            if err in {"access_denied", "expired_token"}:
                self._clear_pending_flow(telegram_id)
                return "expired" if err == "expired_token" else "denied", None, f"Login Google dibatalkan/kedaluwarsa ({err})."
            self._clear_pending_flow(telegram_id)
            return "error", None, f"Error Google OAuth: {tok}"

        access_token = tok.get("access_token")
        if not access_token:
            self._clear_pending_flow(telegram_id)
            return "error", None, "Access token Google tidak ditemukan."

        u_resp = httpx.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )
        u_resp.raise_for_status()
        profile = u_resp.json()
        email = (profile.get("email") or "").strip().lower()
        verified = bool(profile.get("email_verified", False))
        if not email or not verified:
            self._clear_pending_flow(telegram_id)
            return "denied", None, "Email Google belum terverifikasi."

        self._clear_pending_flow(telegram_id)
        return self._bind_google_email(telegram_id, int(flow["chat_id"]), email)

    def _clear_pending_flow(self, telegram_id: int) -> None:
        with self._lock:
            data = self._load()
            data["pending_flows"].pop(str(telegram_id), None)
            self._save(data)

    def _bind_google_email(
        self,
        telegram_id: int,
        chat_id: int,
        email: str,
    ) -> tuple[str, dict[str, Any] | None, str]:
        with self._lock:
            data = self._load()
            users = data["users"]
            record = next(
                (u for u in users if (u.get("google_email") or "").lower() == email and u.get("active", True)),
                None,
            )
            if record is None:
                # Cek env whitelist ALLOWED_GOOGLE_EMAILS jika belum ada di JSON
                allowed_env = [
                    e.strip().lower()
                    for e in (os.environ.get("ALLOWED_GOOGLE_EMAILS") or "").split(",")
                    if e.strip()
                ]
                if email in allowed_env:
                    record = {
                        "google_email": email,
                        "role": "end_user",
                        "telegram_id": telegram_id,
                        "chat_id": chat_id,
                        "active": True,
                        "auth_method": "google_oauth",
                        "authenticated_at": _now_ts(),
                        "session_expires_at": _now_ts() + _session_ttl_seconds(),
                        "created_at": _iso_now(),
                    }
                    users.append(record)
                    self._save(data)
                    return "success", dict(record), f"Login Google berhasil sebagai {email} (End User)."
                return (
                    "denied",
                    None,
                    f"Email Google `{email}` tidak terdaftar di whitelist bot. Minta Administrator menjalankan `/adduser {email} <role>`.",
                )

            record["telegram_id"] = telegram_id
            record["chat_id"] = chat_id
            record["auth_method"] = "google_oauth"
            record["authenticated_at"] = _now_ts()
            record["session_expires_at"] = _now_ts() + _session_ttl_seconds()
            self._save(data)
            role_label = ROLE_LABELS.get(record.get("role", "end_user"), "End User")
            return "success", dict(record), f"Login Google berhasil sebagai `{email}` ({role_label})."
