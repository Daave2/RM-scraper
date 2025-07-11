from __future__ import annotations
import os
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pytz import timezone

LOCAL_TIMEZONE = timezone('Europe/London')

class LocalTimeFormatter(logging.Formatter):
    def converter(self, ts: float):
        dt = datetime.fromtimestamp(ts, LOCAL_TIMEZONE)
        return dt.timetuple()

def setup_logging():
    app_logger = logging.getLogger('app')
    app_logger.setLevel(logging.INFO)
    app_file = RotatingFileHandler('app.log', maxBytes=10**7, backupCount=5)
    fmt = LocalTimeFormatter('%(asctime)s %(levelname)s %(message)s')
    app_file.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    app_logger.addHandler(app_file)
    app_logger.addHandler(console)
    return app_logger

app_logger = setup_logging()

try:
    with open('config.json', 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    app_logger.critical(
        'config.json not found. Please create it from config.example.json before running.'
    )
    exit(1)

DEBUG_MODE = config.get('debug', False)
LOGIN_URL = config.get('login_url', 'https://sellercentral.amazon.co.uk/ap/signin')
CHAT_WEBHOOK_URL = config.get('chat_webhook_url')
SUMMARY_CHAT_WEBHOOK_URL = config.get('summary_chat_webhook_url')
TARGET_STORES = config.get('target_stores', [])

EMOJI_GREEN_CHECK = '\u2705'
EMOJI_RED_CROSS = '\u274C'
COLOR_GOOD = '#2E8B57'
COLOR_BAD = '#CD5C5C'
UPH_THRESHOLD = 80
LATES_THRESHOLD = 3.0
INF_THRESHOLD = 2.0

SMALL_IMAGE_SIZE = 300
QR_CODE_SIZE = 60
WEBHOOK_DELAY_SECONDS = 1.0

JSON_LOG_FILE = os.path.join('output', 'submissions.jsonl')
STORAGE_STATE = 'state.json'
OUTPUT_DIR = 'output'
os.makedirs(OUTPUT_DIR, exist_ok=True)
PAGE_TIMEOUT = 90_000
ACTION_TIMEOUT = 45_000
WAIT_TIMEOUT = 45_000

SCRAPE_RETRY_ATTEMPTS = 3
SCRAPE_RETRY_DELAY = 5
