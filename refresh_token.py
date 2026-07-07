"""
Daily Dhan token refresh — run via cron at 08:45 IST before market open.
Adapted from sohum's refresh_token.py. Uses TOTP to generate a fresh
access token and writes it to .env so the service picks it up automatically
(via importlib.reload in dhan_client.py) without a restart.

Cron entry (EC2, IST = UTC+5:30 → 08:45 IST = 03:15 UTC):
    15 3 * * 1-5 /home/ubuntu/discretionary/venv/bin/python /home/ubuntu/discretionary/refresh_token.py >> /home/ubuntu/discretionary/refresh.log 2>&1
"""
import os
import re
import sys
import time

import pyotp
import requests
from dotenv import dotenv_values

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
DHAN_AUTH_URL = "https://api.dhan.co/v2/token"

MAX_RETRIES = 3
RETRY_DELAY = 30  # seconds


def _send_telegram(token: str, chat_id: str, message: str) -> None:
    if not token or not chat_id:
        print(message)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as exc:
        print(f"[TELEGRAM] {exc}")


def _write_token(new_token: str) -> None:
    """Replace DHAN_ACCESS_TOKEN line in .env, preserving everything else."""
    env = dotenv_values(ENV_PATH)
    env["DHAN_ACCESS_TOKEN"] = new_token

    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            lines = f.readlines()

    updated = False
    new_lines = []
    for line in lines:
        if re.match(r"^\s*DHAN_ACCESS_TOKEN\s*=", line):
            new_lines.append(f"DHAN_ACCESS_TOKEN={new_token}\n")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"DHAN_ACCESS_TOKEN={new_token}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(new_lines)


def refresh() -> bool:
    env = dotenv_values(ENV_PATH)
    client_id   = env.get("DHAN_CLIENT_ID", "")
    pin         = env.get("DHAN_PIN", "")
    totp_secret = env.get("DHAN_TOTP_SECRET", "")
    tg_token    = env.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat     = env.get("TELEGRAM_CHAT_ID", "")

    if not all([client_id, pin, totp_secret]):
        msg = "❌ Token refresh: missing DHAN_CLIENT_ID / DHAN_PIN / DHAN_TOTP_SECRET in .env"
        _send_telegram(tg_token, tg_chat, msg)
        print(msg)
        return False

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            totp = pyotp.TOTP(totp_secret).now()
            resp = requests.post(
                DHAN_AUTH_URL,
                json={"clientId": client_id, "loginType": "API", "pin": pin, "totp": totp},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            new_token = data.get("accessToken") or data.get("access_token") or ""
            if not new_token:
                raise ValueError(f"No token in response: {data}")

            _write_token(new_token)
            msg = "✅ Dhan token refreshed successfully (discretionary service)"
            _send_telegram(tg_token, tg_chat, msg)
            print(msg)
            return True

        except Exception as exc:
            print(f"[attempt {attempt}/{MAX_RETRIES}] Token refresh failed: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    msg = (
        "❌ Dhan token refresh FAILED after all retries.\n"
        "Manual steps:\n"
        "1. Log into Dhan web and copy a fresh access token\n"
        "2. SSH to EC2: <code>nano ~/discretionary/.env</code>\n"
        "3. Update DHAN_ACCESS_TOKEN and save\n"
        "4. The service picks it up automatically on next API call"
    )
    _send_telegram(tg_token, tg_chat, msg)
    print(msg)
    return False


if __name__ == "__main__":
    success = refresh()
    sys.exit(0)  # always exit 0 — Telegram alert is the operator signal
