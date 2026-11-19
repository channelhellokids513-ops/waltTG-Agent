"""
Bot Telegram Interaktif Walt Trading dengan Keamanan Berlapis:
1. Whitelist & Zero-Trust Access Control (blokir user tak dikenal + alert ke Admin)
2. Google OAuth 2.0 Device Flow (/login) + Session Expiry
3. RBAC 3 Role: Administrator (admin), Planner / Financial Advisor (planner), End User (end_user)
4. Setup Strategi Dinamis, Detail Keuangan, Chart Market PNG + Indikator, & Kontrol Eksekusi

Jalankan:
    python telegram_bot.py
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from market_chart import analyze_market_chart, get_actual_price_sources
from strategy_config import (
    STRATEGY_PARAM_SPECS,
    load_strategy_config,
    reset_strategy_config,
    update_strategy_param,
)
from telegram_auth import (
    ROLE_LABELS,
    ROLE_PERMISSIONS,
    AuthManager,
    Role,
    is_google_oauth_configured,
    require_google_auth_strict,
)
import walt_bot


def load_local_env_files() -> None:
    """Muat variabel dari .env dan *.env lokal jika belum ada di os.environ."""
    candidates = [Path(".env")] + sorted(Path(".").glob("*.env"))
    for p in candidates:
        if not p.exists() or p.name == ".env.example":
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and not os.environ.get(k):
                    os.environ[k] = v
        except Exception:
            continue


class TelegramAPI:
    """Wrapper ringan Telegram Bot API berbasis httpx."""

    def __init__(self, token: str):
        token = token.strip()
        if token.startswith("bot"):
            token = token[3:]
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = httpx.Client(timeout=45.0)

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        r = self.client.get(f"{self.base}/getUpdates", params=params, timeout=timeout + 10)
        r.raise_for_status()
        data = r.json()
        return data.get("result") or []

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = "Markdown",
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        if len(text) > 4000:
            text = text[:3980] + "\n\n_...terpotong_"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        r = self.client.post(f"{self.base}/sendMessage", data=payload, timeout=20.0)
        if r.status_code == 400 and parse_mode:
            payload.pop("parse_mode", None)
            self.client.post(f"{self.base}/sendMessage", data=payload, timeout=20.0)

    def send_photo(
        self,
        chat_id: int | str,
        photo_url: str,
        caption: str = "",
        parse_mode: str | None = "Markdown",
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption[:1000],
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            r = self.client.post(f"{self.base}/sendPhoto", data=payload, timeout=25.0)
            if r.status_code == 200:
                return True
            if parse_mode:
                payload.pop("parse_mode", None)
                r2 = self.client.post(f"{self.base}/sendPhoto", data=payload, timeout=25.0)
                return r2.status_code == 200
        except Exception:
            pass
        return False

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.client.post(
                f"{self.base}/answerCallbackQuery",
                data={"callback_query_id": callback_id, "text": text[:180]},
                timeout=10.0,
            )
        except Exception:
            pass


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _build_main_menu(role: str) -> dict[str, Any]:
    rows = [
        [
            {"text": "💰 Keuangan", "callback_data": "cmd:/finance"},
            {"text": "📊 Posisi", "callback_data": "cmd:/positions"},
            {"text": "📈 Chart BTC", "callback_data": "cmd:/chart BTC 1h"},
        ],
        [
            {"text": "⚙️ Strategi", "callback_data": "cmd:/strategy"},
            {"text": "📜 Histori", "callback_data": "cmd:/history"},
            {"text": "🔍 Cek Harga", "callback_data": "cmd:/sources"},
        ],
    ]
    if role == "admin":
        rows.append([
            {"text": "🔎 Scan Market", "callback_data": "cmd:/scan"},
            {"text": "🛡️ Risk Monitor", "callback_data": "cmd:/monitor"},
            {"text": "👥 Users & Audit", "callback_data": "cmd:/users"},
        ])
    return {"inline_keyboard": rows}


class WaltTelegramBot:
    """Router perintah Bot Telegram dengan RBAC, Google Auth, Keuangan, Strategi, & Chart."""

    def __init__(self, api: TelegramAPI, auth: AuthManager | None = None):
        self.api = api
        self.auth = auth or AuthManager()
        self._running_login_polls: set[int] = set()

    def _notify_admin_intrusion(self, tg_id: int, username: str, text: str) -> None:
        self.auth.log_audit("UNAUTHORIZED_ACCESS", tg_id, username=username, detail=text[:120])
        for u in self.auth.list_users():
            if u.get("role") == "admin" and u.get("active", True) and u.get("chat_id"):
                if int(u["chat_id"]) != tg_id:
                    self.api.send_message(
                        u["chat_id"],
                        f"🚨 *Peringatan Keamanan Bot*\n"
                        f"User tidak dikenal mencoba akses:\n"
                        f"• Telegram ID: `{tg_id}`\n"
                        f"• Username: @{username or '-'}\n"
                        f"• Perintah: `{text[:80]}`",
                    )

    def _require(self, tg_id: int, chat_id: int, username: str, perm: str | None) -> dict[str, Any] | None:
        allowed, user, reason = self.auth.authorize(tg_id, permission=perm)
        if not allowed:
            if user is None:
                self._notify_admin_intrusion(tg_id, username, f"perm={perm}")
            else:
                self.auth.log_audit("PERMISSION_DENIED", tg_id, username=username, role=user.get("role", ""), detail=f"perm={perm}")
            self.api.send_message(chat_id, f"🔒 {reason}")
            return None
        return user

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            cb = update["callback_query"]
            cb_id = cb["id"]
            from_u = cb.get("from") or {}
            tg_id = int(from_u["id"])
            username = from_u.get("username") or ""
            msg = cb.get("message") or {}
            chat_id = int((msg.get("chat") or {}).get("id", tg_id))
            data = cb.get("data") or ""
            self.api.answer_callback(cb_id)
            if data.startswith("cmd:"):
                self.handle_text(tg_id, chat_id, username, data[4:])
            elif data.startswith("confirm_close:"):
                asset = data.split(":", 1)[1]
                self._exec_close_asset(tg_id, chat_id, username, asset)
            elif data == "cancel_close":
                self.api.send_message(chat_id, "❎ Penutupan posisi dibatalkan.")
            return

        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text:
            return
        from_u = msg.get("from") or {}
        tg_id = int(from_u["id"])
        username = from_u.get("username") or ""
        chat_id = int((msg.get("chat") or {}).get("id", tg_id))
        self.handle_text(tg_id, chat_id, username, text)

    def handle_text(self, tg_id: int, chat_id: int, username: str, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]

        # Perintah autentikasi terbuka agar user whitelist bisa login via Google Auth
        if cmd == "/login":
            self._cmd_login(tg_id, chat_id, username)
            return
        if cmd == "/logout":
            ok = self.auth.logout(tg_id)
            self.auth.log_audit("LOGOUT", tg_id, username=username, detail=str(ok))
            self.api.send_message(chat_id, "👋 Sesi Anda telah diakhiri." if ok else "ℹ️ Tidak ada sesi aktif.")
            return
        if cmd == "/whoami":
            u = self.auth.get_user_by_telegram(tg_id)
            if not u:
                self._notify_admin_intrusion(tg_id, username, "/whoami")
                self.api.send_message(chat_id, f"🔒 Anda belum terdaftar. Telegram ID Anda: `{tg_id}`")
                return
            role = u.get("role", "end_user")
            exp = int(u.get("session_expires_at") or 0)
            exp_txt = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp)) if exp else "-"
            self.api.send_message(
                chat_id,
                f"🪪 *Profil Akun Aktif*\n"
                f"• Email Google: `{u.get('google_email')}`\n"
                f"• Role: *{ROLE_LABELS.get(role, role)}* (`{role}`)\n"
                f"• Metode Auth: `{u.get('auth_method')}`\n"
                f"• Sesi Berlaku s/d: `{exp_txt}`\n"
                f"• Telegram ID: `{tg_id}`",
            )
            return

        if cmd in {"/start", "/help", "/menu"}:
            user = self._require(tg_id, chat_id, username, "view_finance")
            if not user:
                return
            self._cmd_help(chat_id, user)
            return

        if cmd == "/auth_setup":
            user = self._require(tg_id, chat_id, username, "manage_users")
            if not user:
                return
            self._cmd_auth_setup(chat_id)
            return

        if cmd in {"/status", "/finance"}:
            if not self._require(tg_id, chat_id, username, "view_finance"):
                return
            self._cmd_finance(chat_id)
            return

        if cmd == "/positions":
            if not self._require(tg_id, chat_id, username, "view_finance"):
                return
            self._cmd_positions(chat_id)
            return

        if cmd == "/history":
            if not self._require(tg_id, chat_id, username, "view_finance"):
                return
            self._cmd_history(chat_id)
            return

        if cmd == "/chart":
            if not self._require(tg_id, chat_id, username, "view_chart"):
                return
            asset = args[0] if args else "BTC"
            interval = args[1] if len(args) > 1 else "1h"
            self._cmd_chart(chat_id, asset, interval)
            return

        if cmd == "/price":
            if not self._require(tg_id, chat_id, username, "view_chart"):
                return
            asset = args[0] if args else "BTC"
            self._cmd_price(chat_id, asset)
            return

        if cmd == "/sources":
            if not self._require(tg_id, chat_id, username, "view_chart"):
                return
            asset = args[0] if args else None
            self.api.send_message(chat_id, get_actual_price_sources(asset))
            return

        if cmd == "/strategy":
            user = self._require(tg_id, chat_id, username, "view_strategy")
            if not user:
                return
            self._cmd_strategy(chat_id, user.get("role", "end_user"))
            return

        if cmd == "/set_strategy":
            user = self._require(tg_id, chat_id, username, "edit_strategy")
            if not user:
                return
            if len(args) < 2:
                specs = "\n".join(f"• `{k}` — {v['desc']}" for k, v in STRATEGY_PARAM_SPECS.items())
                self.api.send_message(chat_id, f"Format: `/set_strategy <param> <nilai>`\n\n{specs}")
                return
            try:
                cfg = update_strategy_param(args[0], args[1], updated_by=f"{user.get('google_email')} ({user.get('role')})")
                self.auth.log_audit("UPDATE_STRATEGY", tg_id, username=username, role=user.get("role", ""), detail=f"{args[0]}={args[1]}")
                self.api.send_message(chat_id, f"✅ Parameter `{args[0]}` diperbarui menjadi `{cfg[args[0].lower()]}`.\nGunakan `/strategy` untuk melihat semua konfigurasi.")
            except Exception as e:
                self.api.send_message(chat_id, f"❌ Gagal update strategi: {e}")
            return

        if cmd == "/reset_strategy":
            user = self._require(tg_id, chat_id, username, "edit_strategy")
            if not user:
                return
            reset_strategy_config(updated_by=f"{user.get('google_email')} ({user.get('role')})")
            self.auth.log_audit("RESET_STRATEGY", tg_id, username=username, role=user.get("role", ""))
            self.api.send_message(chat_id, "♻️ Strategi dikembalikan ke standar **True Funding Carry + Asymmetric R:R**.")
            return

        if cmd == "/users":
            if not self._require(tg_id, chat_id, username, "manage_users"):
                return
            self._cmd_users(chat_id)
            return

        if cmd == "/adduser":
            user = self._require(tg_id, chat_id, username, "manage_users")
            if not user:
                return
            if len(args) < 2:
                self.api.send_message(chat_id, "Format: `/adduser <email_google> <admin|planner|end_user> [telegram_id]`")
                return
            try:
                opt_tg = int(args[2]) if len(args) > 2 else None
                rec = self.auth.add_or_update_user(args[0], args[1].lower(), telegram_id=opt_tg)  # type: ignore[arg-type]
                self.auth.log_audit("ADD_USER", tg_id, username=username, role="admin", detail=f"{rec['google_email']} -> {rec['role']}")
                self.api.send_message(chat_id, f"✅ User `{rec['google_email']}` terdaftar sebagai *{ROLE_LABELS[rec['role']]}*.")
            except Exception as e:
                self.api.send_message(chat_id, f"❌ Error: {e}")
            return

        if cmd == "/setrole":
            if not self._require(tg_id, chat_id, username, "manage_users"):
                return
            if len(args) < 2:
                self.api.send_message(chat_id, "Format: `/setrole <email_atau_tg_id> <admin|planner|end_user>`")
                return
            try:
                rec = self.auth.set_role(args[0], args[1].lower())  # type: ignore[arg-type]
                if not rec:
                    self.api.send_message(chat_id, "❌ User tidak ditemukan.")
                    return
                self.auth.log_audit("SET_ROLE", tg_id, username=username, role="admin", detail=f"{args[0]} -> {rec['role']}")
                self.api.send_message(chat_id, f"✅ Role `{rec['google_email']}` diubah menjadi *{ROLE_LABELS[rec['role']]}*.")
            except Exception as e:
                self.api.send_message(chat_id, f"❌ Error: {e}")
            return

        if cmd == "/revoke":
            if not self._require(tg_id, chat_id, username, "manage_users"):
                return
            if not args:
                self.api.send_message(chat_id, "Format: `/revoke <email_atau_tg_id>`")
                return
            rec = self.auth.revoke_user(args[0])
            if not rec:
                self.api.send_message(chat_id, "❌ User tidak ditemukan.")
                return
            self.auth.log_audit("REVOKE_USER", tg_id, username=username, role="admin", detail=args[0])
            self.api.send_message(chat_id, f"🚫 Akses user `{rec['google_email']}` telah dicabut.")
            return

        if cmd == "/audit":
            if not self._require(tg_id, chat_id, username, "view_audit"):
                return
            rows = self.auth.recent_audit(12)
            if not rows:
                self.api.send_message(chat_id, "🛡️ Log audit keamanan masih kosong.")
                return
            lines = [f"• `{r['ts'][:19]}` | *{r['event']}* | ID:`{r['telegram_id']}` | {r.get('detail','')}" for r in rows]
            self.api.send_message(chat_id, "🛡️ *Audit Keamanan Terbaru:*\n" + "\n".join(lines))
            return

        if cmd == "/scan":
            if not self._require(tg_id, chat_id, username, "trade_control"):
                return
            self.api.send_message(chat_id, "🔎 Menjalankan scan funding rate pasar...")
            try:
                res = walt_bot.scan_markets()
                opps = res.get("opportunities") or []
                if not opps:
                    cands = res.get("top_candidates") or []
                    self.api.send_message(chat_id, f"📊 *Hasil Scan:* 0 peluang >= minimal funding.\nTop kandidat: `{cands[:4]}`")
                else:
                    lines = [
                        f"• *{o['base_asset']}* `{o['side'].upper()}` | FR `{o['funding_daily']*100:+.3f}%/hr` | Vol `${o['volume_24h']:,.0f}`"
                        for o in opps[:8]
                    ]
                    self.api.send_message(chat_id, "📊 *Peluang Lolos Filter:*\n" + "\n".join(lines))
            except Exception as e:
                self.api.send_message(chat_id, f"❌ Scan gagal: {e}")
            return

        if cmd == "/monitor":
            if not self._require(tg_id, chat_id, username, "trade_control"):
                return
            self.api.send_message(chat_id, "🛡️ Menjalankan risk monitor manual...")
            try:
                res = walt_bot.risk_monitor()
                self.api.send_message(
                    chat_id,
                    f"✅ Risk Monitor selesai.\n• Posisi dicek: `{res.get('positions_checked', 0)}`\n"
                    f"• Mode: `{res.get('cadence_mode', '-')}`\n• Aksi: `{len(res.get('actions', []))}`",
                )
            except Exception as e:
                self.api.send_message(chat_id, f"❌ Monitor gagal: {e}")
            return

        if cmd == "/execute_market":
            if not self._require(tg_id, chat_id, username, "trade_control"):
                return
            self.api.send_message(chat_id, "🚀 Menjalankan Market Execute (Saham US & Komoditas)...")
            try:
                res = walt_bot.market_execute()
                self.api.send_message(chat_id, f"✅ Selesai: `{res.get('executions_count', 0)}` posisi baru dibuka.")
            except Exception as e:
                self.api.send_message(chat_id, f"❌ Market Execute gagal: {e}")
            return

        if cmd == "/execute_crypto":
            if not self._require(tg_id, chat_id, username, "trade_control"):
                return
            self.api.send_message(chat_id, "⚡ Menjalankan Crypto Execute (Bluechip)...")
            try:
                res = walt_bot.crypto_execute()
                self.api.send_message(chat_id, f"✅ Selesai: `{res.get('executions_count', 0)}` posisi crypto baru dibuka.")
            except Exception as e:
                self.api.send_message(chat_id, f"❌ Crypto Execute gagal: {e}")
            return

        if cmd == "/close":
            if not self._require(tg_id, chat_id, username, "trade_control"):
                return
            if not args:
                self.api.send_message(chat_id, "Format: `/close <ASSET>` (contoh: `/close SPY`)")
                return
            asset = args[0].upper().strip()
            kb = {
                "inline_keyboard": [[
                    {"text": f"✅ Ya, Tutup {asset}", "callback_data": f"confirm_close:{asset}"},
                    {"text": "❌ Batal", "callback_data": "cancel_close"},
                ]]
            }
            self.api.send_message(chat_id, f"⚠️ Konfirmasi penutupan posisi *{asset}* pada harga pasar?", reply_markup=kb)
            return

        # Perintah tidak dikenal → tetap cek otorisasi terlebih dahulu
        if self._require(tg_id, chat_id, username, "view_finance"):
            self.api.send_message(chat_id, "❓ Perintah tidak dikenal. Ketik `/help` untuk melihat menu.")

    # -------------------------------------------------------------------------
    # HANDLER IMPLEMENTATIONS
    # -------------------------------------------------------------------------

    def _cmd_login(self, tg_id: int, chat_id: int, username: str) -> None:
        if not is_google_oauth_configured():
            self.api.send_message(
                chat_id,
                "⚠️ *Google OAuth belum dikonfigurasi di server bot.*\n"
                "Untuk mengaktifkan login Google Auth:\n"
                "1. Buat OAuth Client ID (tipe *TV and Limited Input devices* atau *Desktop*) di Google Cloud Console.\n"
                "2. Isi `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, dan `ADMIN_GOOGLE_EMAIL` di `.env`.\n"
                "3. Restart bot dan jalankan `/login` lagi.\n\n"
                f"Saat ini Telegram ID Anda (`{tg_id}`) tetap terlindungi oleh whitelist bot.",
            )
            return

        if tg_id in self._running_login_polls:
            self.api.send_message(chat_id, "⏳ Proses login Google sebelumnya masih aktif. Selesaikan di browser Anda.")
            return

        try:
            flow = self.auth.start_google_device_login(tg_id, chat_id)
        except Exception as e:
            self.api.send_message(chat_id, f"❌ Gagal memulai Google OAuth: {e}")
            return

        self.api.send_message(
            chat_id,
            f"🔐 *Login Google Auth*\n"
            f"1. Buka link: {flow['verification_url']}\n"
            f"2. Masukkan kode verifikasi: `{flow['user_code']}`\n"
            f"3. Login dengan akun Google Anda.\n\n"
            f"_Bot sedang menunggu konfirmasi (berlaku ~{int(flow['expires_in']//60)} menit)..._",
        )

        def _poll_loop() -> None:
            self._running_login_polls.add(tg_id)
            try:
                interval = max(5, int(flow.get("interval", 5)))
                deadline = time.time() + int(flow.get("expires_in", 900))
                while time.time() < deadline:
                    time.sleep(interval)
                    status, rec, msg = self.auth.poll_google_device_login(tg_id)
                    if status == "pending":
                        continue
                    if status == "success" and rec:
                        self.auth.log_audit("GOOGLE_LOGIN_OK", tg_id, username=username, role=rec.get("role", ""), detail=rec.get("google_email", ""))
                        self.api.send_message(chat_id, f"✅ {msg}", reply_markup=_build_main_menu(rec.get("role", "end_user")))
                    else:
                        self.auth.log_audit("GOOGLE_LOGIN_FAIL", tg_id, username=username, detail=msg)
                        self.api.send_message(chat_id, f"❌ {msg}")
                    break
            finally:
                self._running_login_polls.discard(tg_id)

        threading.Thread(target=_poll_loop, daemon=True).start()

    def _cmd_auth_setup(self, chat_id: int) -> None:
        g_ok = "✅ Aktif" if is_google_oauth_configured() else "⚠️ Belum diisi (`GOOGLE_OAUTH_CLIENT_ID`)"
        strict = "✅ Ketat (`REQUIRE_GOOGLE_AUTH=true`)" if require_google_auth_strict() else "🟡 Mode Transisi (Bootstrap Owner + Whitelist)"
        self.api.send_message(
            chat_id,
            f"🛡️ *Status Konfigurasi Keamanan*\n"
            f"• Google OAuth 2.0: {g_ok}\n"
            f"• Kebijakan Auth: {strict}\n"
            f"• Total User Terdaftar: `{len(self.auth.list_users())}`\n\n"
            f"Gunakan `/adduser <email> <admin|planner|end_user> [tg_id]` untuk menambah user baru.",
        )

    def _cmd_help(self, chat_id: int, user: dict[str, Any]) -> None:
        role: Role = user.get("role", "end_user")
        perms = ROLE_PERMISSIONS.get(role, set())
        lines = [
            f"🤖 *Walt Trading Bot — Menu Utama*",
            f"Akun: `{user.get('google_email')}` | Role: *{ROLE_LABELS.get(role, role)}*\n",
            "💰 *Keuangan & Market (Semua Role):*",
            "• `/finance` / `/status` — Ringkasan saldo, equity, margin & PnL",
            "• `/positions` — Detail posisi terbuka (SL/TP/PnL/Funding)",
            "• `/history` — Riwayat penutupan posisi terbaru",
            "• `/chart <ASET> [15m|1h|4h|1d]` — Chart visual + RSI & EMA",
            "• `/price <ASET>` — Harga aktual & funding rate di Walt",
            "• `/sources [ASET]` — Rekomendasi situs cek harga aktual",
            "• `/strategy` — Lihat parameter strategi aktif",
            "• `/whoami`, `/login`, `/logout` — Info & sesi Google Auth",
        ]
        if "edit_strategy" in perms:
            lines += [
                "\n⚙️ *Planner & Admin (Setup Strategi):*",
                "• `/set_strategy <param> <nilai>` — Ubah SL/TP/Min Funding/Cooldown",
                "• `/reset_strategy` — Kembalikan ke default True Funding Carry",
            ]
        if "manage_users" in perms:
            lines += [
                "\n👑 *Khusus Administrator:*",
                "• `/users`, `/adduser`, `/setrole`, `/revoke`, `/audit`, `/auth_setup`",
                "• `/scan`, `/monitor`, `/execute_market`, `/execute_crypto`, `/close <ASET>`",
            ]
        self.api.send_message(chat_id, "\n".join(lines), reply_markup=_build_main_menu(role))

    def _cmd_finance(self, chat_id: int) -> None:
        try:
            client = walt_bot.WaltClient()
            bal = client.balance()
            pos_raw = client.list_positions(statuses=["open"])
            avail = walt_bot.get_available_balance(bal)
            equity = walt_bot.get_total_equity(bal, pos_raw)
            positions = pos_raw.get("positions", []) if isinstance(pos_raw, dict) else (pos_raw or [])
            open_pos = [p for p in positions if isinstance(p, dict) and str(p.get("status", "open")).lower() in ("open", "active")]

            used_margin = sum(_safe_float(p.get("margin_quote", p.get("margin", 0.0))) for p in open_pos)
            unrealized = sum(_safe_float(p.get("unrealized_pnl", p.get("pnl_quote", 0.0))) for p in open_pos)
            margin_pct = (used_margin / equity * 100.0) if equity > 0 else 0.0
            max_slots = walt_bot.get_dynamic_max_slots(equity)

            # Realized stats dari closed_history.csv
            closed_count = 0
            wins = 0
            realized_sum = 0.0
            csv_path = Path("logs") / "closed_history.csv"
            if csv_path.exists():
                with csv_path.open("r", encoding="utf-8", errors="ignore") as f:
                    for row in csv.DictReader(f):
                        pnl = _safe_float(row.get("pnl_usd", row.get("realized_pnl", 0.0)))
                        closed_count += 1
                        realized_sum += pnl
                        if pnl > 0:
                            wins += 1
            win_rate = (wins / closed_count * 100.0) if closed_count else 0.0

            msg = (
                f"💰 *Laporan Kondisi Keuangan — Walt Perps*\n"
                f"• Total Equity: `${equity:,.4f}`\n"
                f"• Saldo Tersedia (Available): `${avail:,.4f}`\n"
                f"• Margin Terpakai: `${used_margin:,.4f}` (`{margin_pct:.1f}%`)\n"
                f"• Unrealized PnL Aktif: `${unrealized:+,.4f}`\n"
                f"• Slot Posisi: `{len(open_pos)}/{max_slots}`\n\n"
                f"📈 *Statistik Histori Lokal (`closed_history.csv`):*\n"
                f"• Total Trade Tertutup: `{closed_count}` (Win Rate `{win_rate:.1f}%`)\n"
                f"• Akumulasi Realized PnL: `${realized_sum:+,.4f}`"
            )
            self.api.send_message(chat_id, msg)
        except Exception as e:
            self.api.send_message(chat_id, f"❌ Gagal memuat data keuangan Walt: {e}")

    def _cmd_positions(self, chat_id: int) -> None:
        try:
            client = walt_bot.WaltClient()
            pos_raw = client.list_positions(statuses=["open"])
            positions = pos_raw.get("positions", []) if isinstance(pos_raw, dict) else (pos_raw or [])
            open_pos = [p for p in positions if isinstance(p, dict) and str(p.get("status", "open")).lower() in ("open", "active")]
            if not open_pos:
                self.api.send_message(chat_id, "📭 Saat ini tidak ada posisi terbuka (0 posisi aktif).")
                return
            lines = ["📊 *Detail Posisi Terbuka:*"]
            for p in open_pos:
                asset = p.get("base_asset") or p.get("asset") or "?"
                side = str(p.get("side", "?")).upper()
                entry = _safe_float(p.get("entry_price"))
                cur = _safe_float(p.get("current_price", p.get("mark_price", entry)))
                margin = _safe_float(p.get("margin_quote", p.get("margin", 0.0)))
                pnl = _safe_float(p.get("unrealized_pnl", p.get("pnl_quote", 0.0)))
                sl = _safe_float(p.get("sl_price", p.get("stop_loss", 0.0)))
                tp = _safe_float(p.get("tp_price", p.get("take_profit", 0.0)))
                pnl_pct = (pnl / margin * 100.0) if margin > 0 else 0.0
                lines.append(
                    f"\n• *{asset}* `{side}` | Margin `${margin:.2f}`\n"
                    f"  Entry `${entry:,.4f}` → Now `${cur:,.4f}`\n"
                    f"  PnL: `${pnl:+.4f}` (`{pnl_pct:+.2f}%`)\n"
                    f"  SL: `${sl:,.4f}` | TP: `${tp:,.4f}`"
                )
            self.api.send_message(chat_id, "\n".join(lines))
        except Exception as e:
            self.api.send_message(chat_id, f"❌ Gagal mengambil posisi: {e}")

    def _cmd_history(self, chat_id: int) -> None:
        csv_path = Path("logs") / "closed_history.csv"
        if not csv_path.exists():
            self.api.send_message(chat_id, "📜 Belum ada histori penutupan posisi di `logs/closed_history.csv`.")
            return
        with csv_path.open("r", encoding="utf-8", errors="ignore") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            self.api.send_message(chat_id, "📜 Histori penutupan posisi masih kosong.")
            return
        lines = ["📜 *10 Penutupan Posisi Terbaru:*"]
        for r in reversed(rows[-10:]):
            asset = r.get("asset") or r.get("base_asset") or "?"
            side = (r.get("side") or "?").upper()
            pnl = _safe_float(r.get("pnl_usd", r.get("realized_pnl", 0.0)))
            reason = r.get("close_reason") or r.get("reason") or "-"
            ts = (r.get("closed_at") or r.get("ts") or "-")[:16]
            lines.append(f"• `{ts}` *{asset}* `{side}` | `${pnl:+.4f}` | _{reason}_")
        self.api.send_message(chat_id, "\n".join(lines))

    def _cmd_chart(self, chat_id: int, asset: str, interval: str) -> None:
        info = analyze_market_chart(asset, interval)
        if not info.get("ok"):
            self.api.send_message(chat_id, f"❌ Gagal memuat chart untuk `{asset.upper()}`.")
            return

        walt_line = ""
        try:
            contracts = walt_bot.WaltClient().list_contracts()
            items = contracts.get("contracts", contracts) if isinstance(contracts, dict) else contracts
            match = next((c for c in (items or []) if str(c.get("base_asset", "")).upper() == info["asset"]), None)
            if match:
                fr_daily = _safe_float(match.get("funding_rate_hourly")) * 24 * 100
                vol = _safe_float(match.get("volume_24h"))
                walt_line = f"\n• Funding Walt: `{fr_daily:+.3f}%/hari` | Vol 24h `${vol:,.0f}`"
        except Exception:
            pass

        rsi_txt = f"{info['rsi14']}" if info["rsi14"] is not None else "-"
        caption = (
            f"📈 *Market Chart {info['asset']} ({info['interval']})*\n"
            f"• Sumber: `{info['source']}`\n"
            f"• Harga: `${info['last_price']:,.4f}` (`{info['change_pct']:+.2f}%`)\n"
            f"• Range: `${info['low']:,.4f}` – `${info['high']:,.4f}`\n"
            f"• EMA20 / EMA50: `${info['ema20']:,.4f}` / `${info['ema50']:,.4f}`\n"
            f"• RSI-14: `{rsi_txt}` | Trend: *{info['trend']}*\n"
            f"• Sparkline: `{info['sparkline']}`"
            f"{walt_line}"
        )
        sent_img = self.api.send_photo(chat_id, info["chart_url"], caption=caption)
        if not sent_img:
            self.api.send_message(chat_id, caption)
        self.api.send_message(chat_id, get_actual_price_sources(info["asset"]))

    def _cmd_price(self, chat_id: int, asset: str) -> None:
        sym = asset.upper().strip()
        try:
            contracts = walt_bot.WaltClient().list_contracts()
            items = contracts.get("contracts", contracts) if isinstance(contracts, dict) else contracts
            match = next((c for c in (items or []) if str(c.get("base_asset", "")).upper() == sym), None)
            if not match:
                self.api.send_message(chat_id, f"ℹ️ Aset `{sym}` tidak ditemukan di daftar kontrak Walt.\n\n{get_actual_price_sources(sym)}")
                return
            mark = _safe_float(match.get("mark_price", match.get("index_price", match.get("last_price"))))
            fr_daily = _safe_float(match.get("funding_rate_hourly")) * 24 * 100
            vol = _safe_float(match.get("volume_24h"))
            rec_side = "SHORT" if fr_daily > 0 else "LONG"
            self.api.send_message(
                chat_id,
                f"💲 *Harga & Funding Aktual Walt ({sym})*\n"
                f"• Mark Price: `${mark:,.4f}`\n"
                f"• Funding Rate: `{fr_daily:+.3f}%/hari` (Saran Arah: `{rec_side}`)\n"
                f"• Volume 24h: `${vol:,.0f}`\n\n"
                f"{get_actual_price_sources(sym)}",
            )
        except Exception as e:
            self.api.send_message(chat_id, f"❌ Gagal mengambil harga Walt: {e}")

    def _cmd_strategy(self, chat_id: int, role: str) -> None:
        cfg = load_strategy_config()
        editable = "✅ Anda dapat mengubah lewat `/set_strategy <param> <nilai>`." if role in {"admin", "planner"} else "🔒 Mode baca saja (khusus Admin/Planner)."
        msg = (
            f"⚙️ *Konfigurasi Strategi Aktif (True Funding Carry)*\n"
            f"• `default_sl_pct`: `{cfg['default_sl_pct']*100:.2f}%` (`{cfg['default_sl_pct']}`)\n"
            f"• `tp_calm_pct` (ETF & Crypto): `{cfg['tp_calm_pct']*100:.2f}%` (`{cfg['tp_calm_pct']}`)\n"
            f"• `tp_volatile_pct` (Komoditas & Saham): `{cfg['tp_volatile_pct']*100:.2f}%` (`{cfg['tp_volatile_pct']}`)\n"
            f"• `min_funding_daily`: `{cfg['min_funding_daily']*100:.2f}%/hari` (`{cfg['min_funding_daily']}`)\n"
            f"• `reentry_cooldown_hours`: `{cfg['reentry_cooldown_hours']} jam`\n"
            f"• `max_slots_cap`: `{cfg['max_slots_cap']}`\n"
            f"• `dry_run`: `{cfg['dry_run']}`\n\n"
            f"Diubah terakhir oleh: `{cfg.get('updated_by', 'default')}`\n"
            f"{editable}"
        )
        self.api.send_message(chat_id, msg)

    def _cmd_users(self, chat_id: int) -> None:
        users = self.auth.list_users()
        if not users:
            self.api.send_message(chat_id, "👥 Belum ada user terdaftar.")
            return
        lines = ["👥 *Daftar User & Role RBAC:*"]
        for u in users:
            st = "🟢" if u.get("active", True) else "🔴"
            lines.append(
                f"{st} `{u.get('google_email')}` | *{ROLE_LABELS.get(u.get('role','end_user'))}* | TG:`{u.get('telegram_id')}` | `{u.get('auth_method')}`"
            )
        lines.append("\nPerintah: `/adduser`, `/setrole`, `/revoke`, `/audit`")
        self.api.send_message(chat_id, "\n".join(lines))

    def _exec_close_asset(self, tg_id: int, chat_id: int, username: str, asset: str) -> None:
        if not self._require(tg_id, chat_id, username, "trade_control"):
            return
        try:
            client = walt_bot.WaltClient()
            pos_raw = client.list_positions(statuses=["open"])
            positions = pos_raw.get("positions", []) if isinstance(pos_raw, dict) else (pos_raw or [])
            target = next(
                (p for p in positions if str(p.get("base_asset", "")).upper() == asset.upper() and str(p.get("status", "open")).lower() in ("open", "active")),
                None,
            )
            if not target:
                self.api.send_message(chat_id, f"ℹ️ Posisi terbuka untuk `{asset}` tidak ditemukan.")
                return
            pos_id = target.get("position_id") or target.get("id")
            client.auto_close(pos_id, reason="Manual close via Telegram Admin")
            self.auth.log_audit("MANUAL_CLOSE", tg_id, username=username, role="admin", detail=f"{asset} ({pos_id})")
            self.api.send_message(chat_id, f"✅ Perintah tutup posisi `{asset}` telah dikirim ke Walt.")
        except Exception as e:
            self.api.send_message(chat_id, f"❌ Gagal menutup `{asset}`: {e}")


def run_polling() -> None:
    load_local_env_files()
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token or token.lower() == "dummy":
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum disetel.")
    api = TelegramAPI(token)
    auth = AuthManager()
    bot = WaltTelegramBot(api=api, auth=auth)
    print("[INFO] Walt Telegram Bot aktif (long-polling + RBAC + Google Auth siap)...")

    offset: int | None = None
    while True:
        try:
            updates = api.get_updates(offset=offset, timeout=25)
            for upd in updates:
                offset = int(upd["update_id"]) + 1
                bot.handle_update(upd)
        except KeyboardInterrupt:
            print("[INFO] Bot dihentikan.")
            break
        except Exception as e:
            print(f"[WARN] Polling error: {e}")
            time.sleep(3)


if __name__ == "__main__":
    run_polling()
