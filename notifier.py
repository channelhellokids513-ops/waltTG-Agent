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
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[WARN] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum di-set, skip notif")
        print(f"--- MESSAGE ---\n{text}\n---")
        return False

    # Telegram max message = 4096 chars
    if len(text) > 4000:
        text = text[:4000] + "\n\n_...truncated_"

    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=15.0,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")
        return False


if __name__ == "__main__":
    ok = send_telegram("*Test* dari walt-trading-bot ✅")
    print(f"Sent: {ok}")
