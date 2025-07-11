from __future__ import annotations
import os
import json
from datetime import datetime
from playwright.async_api import Page
from .settings import OUTPUT_DIR, STORAGE_STATE, LOCAL_TIMEZONE, app_logger

async def save_screenshot(page: Page | None, prefix: str) -> None:
    if not page or page.is_closed():
        return
    try:
        path = os.path.join(
            OUTPUT_DIR,
            f"{prefix}_{datetime.now(LOCAL_TIMEZONE).strftime('%Y%m%d_%H%M%S')}.png"
        )
        await page.screenshot(path=path, full_page=True, timeout=15000)
        app_logger.info(f"Screenshot saved: {path}")
    except Exception as e:
        app_logger.error(f"Screenshot error: {e}")

def ensure_storage_state() -> bool:
    if not os.path.exists(STORAGE_STATE) or os.path.getsize(STORAGE_STATE) == 0:
        return False
    try:
        with open(STORAGE_STATE, 'r') as f:
            data = json.load(f)
        return isinstance(data, dict) and data.get('cookies')
    except (json.JSONDecodeError, IOError):
        return False
