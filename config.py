import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)

DHAN_CLIENT_ID      = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN   = os.getenv("DHAN_ACCESS_TOKEN", "")
DHAN_PIN            = os.getenv("DHAN_PIN", "")
DHAN_TOTP_SECRET    = os.getenv("DHAN_TOTP_SECRET", "")

TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")

WEBHOOK_SECRET      = os.getenv("WEBHOOK_SECRET", "")
TRADE_QTY           = int(os.getenv("TRADE_QTY", "1"))   # lots; 1 lot = 65 qty
STRADDLE_LOTS       = int(os.getenv("STRADDLE_LOTS", "10"))       # 9:15 straddle size (lots per leg)
STRADDLE_LOTS_1400  = int(os.getenv("STRADDLE_LOTS_1400", "10"))  # 2pm expiry straddle size (lots per leg)
POLL_INTERVAL       = int(os.getenv("POLL_INTERVAL", "30"))
ENTRY_COOLDOWN      = int(os.getenv("ENTRY_COOLDOWN", "60"))  # secs; bridges order → positions visibility
FLASK_PORT          = int(os.getenv("FLASK_PORT", "5001"))

LOT_SIZE            = 65
