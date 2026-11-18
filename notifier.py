"""
Telegram bot notifier untuk hasil scheduled run.

Butuh env:
    TELEGRAM_BOT_TOKEN — dari @BotFather
    TELEGRAM_CHAT_ID   — chat ID kamu (bisa dari @userinfobot)
"""

import os
import httpx


def send_telegram(text: str, parse_mode: str = "Markdown") -> bool:
    """Kirim pesan ke Telegram. Return True kalau sukses."""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id or token.lower() == "dummy" or chat_id.lower() == "dummy":
        print("[INFO] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID masih 'dummy' atau belum di-set, skip notif Telegram.")
        print(f"--- PREVIEW PESAN ---\n{text}\n---------------------")
        return False

    # Bersihkan prefix 'bot' jika pengguna tidak sengaja menyertakannya
    if token.startswith("bot"):
        token = token[3:]

    # Telegram max message = 4096 chars
    if len(text) > 4000:
        text = text[:4000] + "\n\n_...truncated_"

    import time
    for attempt in range(1, 4):
        try:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode

            r = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                timeout=15.0,
            )
            if r.status_code == 400:
                if parse_mode:
                    print("[WARN] Telegram HTTP 400 dengan parse_mode, retry sekali tanpa parse_mode...")
                    r_plain = httpx.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data={
                            "chat_id": chat_id,
                            "text": text,
                            "disable_web_page_preview": True,
                        },
                        timeout=15.0,
                    )
                    if r_plain.status_code == 200:
                        return True
                    print(f"[ERROR] Telegram plain fallback gagal (HTTP {r_plain.status_code}): {r_plain.text}")
                else:
                    print(f"[ERROR] Telegram HTTP 400: {r.text}")
                return False

            r.raise_for_status()
            return True
        except Exception as e:
            if attempt == 3:
                print(f"[ERROR] Telegram send failed (attempt {attempt}/3): {e}")
                return False
            time.sleep(1.5 * attempt)
    return False


if __name__ == "__main__":
    ok = send_telegram("*Test* dari walt-trading-bot ✅")
    print(f"Sent: {ok}")
