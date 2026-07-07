"""Outbound-only Telegram sender. Never raises — bot must keep running if Telegram is down."""
import logging
import requests

log = logging.getLogger(__name__)


def send(message: str) -> None:
    import config
    # Read credentials directly from already-loaded config module
    # (dhan_client handles .env reload; Telegram token rarely changes)
    token   = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        print(f"[TELEGRAM-DISABLED] {message}")
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
        if not resp.ok:
            log.warning("[TELEGRAM] send failed: %s", resp.text)
    except Exception as exc:
        log.warning("[TELEGRAM] send exception: %s", exc)
