"""Lead Intake configuration — loads from /opt/hr-radar/.env"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Bitrix24
B24_WEBHOOK_URL = os.environ["B24_WEBHOOK_URL"].rstrip("/")

# Telegram
TG_BOT_TOKEN = os.environ["LEAD_TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ.get("LEAD_TG_CHAT_ID", "64605499")

# OpenAI
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# IMAP (for email body fetching)
IMAP_HOST = os.environ.get("LEAD_IMAP_HOST", "imap.yandex.ru")
IMAP_USER = os.environ.get("LEAD_IMAP_USER", "client@btl-agency.ru")
IMAP_PASS = os.environ.get("LEAD_IMAP_PASS", "")

# Polling
POLL_INTERVAL_SEC = int(os.environ.get("LEAD_POLL_INTERVAL", "300"))  # 5 min

# Paths
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "lead_intake.db"
AUDIO_DIR = DATA_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)

# Feature flags
AUTO_UPDATE_STATUS = os.environ.get("LEAD_AUTO_STATUS", "0") == "1"  # Set to 1 to auto-change B24 statuses
DISABLE_IMAP = os.environ.get("LEAD_DISABLE_IMAP", "0") == "1"  # Set to 1 to disable IMAP fetching
DEBUG_SKIPPED = os.environ.get("LEAD_DEBUG_SKIPPED", "1") == "1"  # Show skipped items in TG
