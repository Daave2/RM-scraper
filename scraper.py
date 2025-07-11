# =======================================================================================
#         AMAZON SELLER CENTRAL SCRAPER (MULTI-STORE VERSION)
# =======================================================================================
# - Handles multiple stores from a list in the configuration.
# - Posts individual store reports and a final aggregate summary to webhooks.
# - Uses robust session management to log in only when necessary.
# =======================================================================================

import logging
import urllib.parse
from datetime import datetime
from pytz import timezone
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError,
    expect,
)
import os
import json
import asyncio
import pyotp
from logging.handlers import RotatingFileHandler
import re
import aiohttp
import aiofiles
import ssl
import certifi

# Use UK timezone for log timestamps
LOCAL_TIMEZONE = timezone('Europe/London')

class LocalTimeFormatter(logging.Formatter):
    def converter(self, ts: float):
        dt = datetime.fromtimestamp(ts, LOCAL_TIMEZONE)
        return dt.timetuple()

# --- Logging Setup ---
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

# --- Config & Constants ---
try:
    with open('config.json', 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    app_logger.critical("config.json not found. Please create it from config.example.json before running.")
    exit(1)

DEBUG_MODE               = config.get('debug', False)
LOGIN_URL                = config.get('login_url')
CHAT_WEBHOOK_URL         = config.get('chat_webhook_url')
SUMMARY_CHAT_WEBHOOK_URL = config.get('summary_chat_webhook_url')
TARGET_STORES            = config.get('target_stores', []) 

# --- Emojis and Colors for Chat ---
EMOJI_GREEN_CHECK = "\u2705"  # ✅
EMOJI_RED_CROSS   = "\u274C"  # ❌
COLOR_GOOD        = "#2E8B57" 
COLOR_BAD         = "#CD5C5C"

UPH_THRESHOLD    = 80
LATES_THRESHOLD = 3.0
INF_THRESHOLD   = 2.0

JSON_LOG_FILE = os.path.join('output', 'submissions.jsonl')
STORAGE_STATE  = 'state.json'
OUTPUT_DIR     = 'output'
os.makedirs(OUTPUT_DIR, exist_ok=True)

PAGE_TIMEOUT       = 90_000
ACTION_TIMEOUT     = 45_000
WAIT_TIMEOUT       = 45_000
WORKER_RETRY_COUNT = 3

playwright = None
browser    = None
log_lock   = asyncio.Lock()


# =======================================================================================
#    AUTHENTICATION & SESSION MANAGEMENT
# =======================================================================================

async def _save_screenshot(page: Page | None, prefix: str):
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
        return isinstance(data, dict) and data.get("cookies")
    except (json.JSONDecodeError, IOError):
        return False

async def check_if_login_needed(page: Page, test_url: str) -> bool:
    try:
        await page.goto(test_url, timeout=PAGE_TIMEOUT, wait_until="load")
        if "signin" in page.url.lower() or "/ap/" in page.url:
            app_logger.info("Session invalid, login required.")
            return True
        await expect(page.locator("#range-selector")).to_be_visible(timeout=WAIT_TIMEOUT)
        app_logger.info("Existing session still valid.")
        return False
    except Exception:
        app_logger.warning("Error verifying session; assuming login required.")
        return True

async def perform_login(page: Page) -> bool:
    app_logger.info("Starting login flow")
    try:
        await page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT, wait_until="load")
        await page.get_by_label("Email or mobile phone number").fill(config['login_email'])
        await page.get_by_label("Continue").click()

        pw = page.get_by_label("Password")
        await expect(pw).to_be_visible(timeout=WAIT_TIMEOUT)
        await pw.fill(config['login_password'])
        await page.get_by_label("Sign in").click()

        otp_sel  = 'input[id*="otp"]'
        dash_sel = "#content"
        await page.wait_for_selector(f"{otp_sel}, {dash_sel}", timeout=WAIT_TIMEOUT)

        if await page.locator(otp_sel).is_visible():
            code = pyotp.TOTP(config['otp_secret_key']).now()
            await page.locator(otp_sel).fill(code)
            await page.get_by_role("button", name="Sign in").click()
        
        await expect(page.locator(dash_sel)).to_be_visible(timeout=WAIT_TIMEOUT)
        app_logger.info("Login successful.")
        return True
    except Exception as e:
        app_logger.critical(f"Login failed: {e}", exc_info=DEBUG_MODE)
        await _save_screenshot(page, "login_failure")
        return False

async def prime_master_session() -> bool:
    global browser
    app_logger.info("Priming master session")
    ctx = await browser.new_context()
    try:
        page = await ctx.new_page()
        if not await perform_login(page):
            return False
        await ctx.storage_state(path=STORAGE_STATE)
        app_logger.info("Saved new session state.")
        return True
    finally:
        await ctx.close()


# =======================================================================================
#                       UTILITIES & CORE SCRAPING LOGIC
# =======================================================================================

def _format_metric_with_emoji(value_str: str, threshold: float, is_uph: bool = False) -> str:
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        emoji = EMOJI_GREEN_CHECK if is_good else EMOJI_RED_CROSS
        return f"{value_str} {emoji}"
    except (ValueError, TypeError):
        return value_str
        
def _format_metric_with_color(value_str: str, threshold: float, is_uph: bool = False) -> str:
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        color = COLOR_GOOD if is_good else COLOR_BAD
        return f'<font color="{color}">{value_str}</font>'
    except (ValueError, TypeError):
        return value_str

async def log_results(data: dict):
    async with log_lock:
        log_entry = {'timestamp': datetime.now(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'), **data}
        try:
            async with aiofiles.open(JSON_LOG_FILE, 'a', encoding='utf-8') as f:
                await f.write(json.dumps(log_entry) + '\n')
        except IOError as e:
            app_logger.error(f"Error writing to JSON log file {JSON_LOG_FILE}: {e}")

async def scrape_store_data(browser: Browser, store_info: dict, storage_state: dict) -> dict | None:
    store_name = store_info['store_name']
    app_logger.info(f"Starting detailed data collection for '{store_name}'")

    for attempt in range(WORKER_RETRY_COUNT):
        ctx: BrowserContext = None
        try:
            ctx = await browser.new_context(storage_state=storage_state)
            page = await ctx.new_page()

            dash_url = (
                f"https://sellercentral.amazon.co.uk/snowdash"
                f"?mons_sel_dir_mcid={store_info['merchant_id']}"
                f"&mons_sel_mkid={store_info['marketplace_id']}"
            )
            await page.goto(dash_url, timeout=PAGE_TIMEOUT)

            refresh_button = page.get_by_role("button", name="Refresh")
            await expect(refresh_button).to_be_visible(timeout=WAIT_TIMEOUT)

            customised_tab = page.locator("#content span:has-text('Customised')").nth(0)
            await customised_tab.click(timeout=ACTION_TIMEOUT)
            
            date_picker = page.locator("kat-date-range-picker")
            await expect(date_picker).to_be_visible(timeout=WAIT_TIMEOUT)
            
            now = datetime.now(LOCAL_TIMEZONE).strftime("%m/%d/%Y")
            date_inputs = date_picker.locator('input[type="text"]')
            await date_inputs.nth(0).fill(now)
            await date_inputs.nth(1).fill(now)

            apply_btn = page.get_by_role("button", name="Apply")
            async with page.expect_response(lambda r: "/api/metrics" in r.url, timeout=30000):
                await apply_btn.click(timeout=ACTION_TIMEOUT)

            async with page.expect_response(lambda r: "/api/metrics" in r.url, timeout=40000) as refresh_info:
                await refresh_button.click(timeout=ACTION_TIMEOUT)
            api_data = await (await refresh_info.value).json()
            app_logger.info(f"Received /api/metrics response for {store_name}.")
            
            shopper_stats = []
            store_total_units_picked = store_total_pick_time_sec = store_total_orders = 0
            store_total_requested_units = store_total_items_not_found = store_total_weighted_lates = 0

            for entry in api_data:
                metrics = entry.get("metrics", {})
                shopper_name = entry.get("shopperName")
                if entry.get("type") == "MASTER" and shopper_name and shopper_name != "SHOPPER_NAME_NOT_FOUND":
                    orders = metrics.get("OrdersShopped_V2", 0)
                    if orders == 0: continue
                    
                    units = metrics.get("PickedUnits_V2", 0)
                    pick_time_sec = metrics.get("PickTimeInSec_V2", 0)
                    uph = (units / (pick_time_sec / 3600)) if pick_time_sec > 0 else 0.0
                    shopper_stats.append({
                        "name": shopper_name, "uph": f"{uph:.0f}",
                        "inf": f"{metrics.get('ItemNotFoundRate_V2', 0.0):.1f} %",
                        "lates": f"{metrics.get('LatePicksRate', 0.0):.1f} %", "orders": int(orders),
                    })
                    store_total_units_picked += units
                    store_total_pick_time_sec += pick_time_sec
                    store_total_orders += orders
                    req_units = metrics.get("RequestedQuantity_V2", 0)
                    inf_rate = metrics.get("ItemNotFoundRate_V2", 0.0) / 100.0
                    store_total_requested_units += req_units
                    store_total_items_not_found += req_units * inf_rate
                    lates_rate = metrics.get("LatePicksRate", 0.0) / 100.0
                    store_total_weighted_lates += lates_rate * orders

            if not shopper_stats:
                app_logger.warning(f"No active shoppers found for {store_name}.")
                return {"overall": {'store': store_name}, "shoppers": []}

            overall_uph = (store_total_units_picked / (store_total_pick_time_sec / 3600)) if store_total_pick_time_sec > 0 else 0
            overall_inf = (store_total_items_not_found / store_total_requested_units) * 100 if store_total_requested_units > 0 else 0
            overall_lates = (store_total_weighted_lates / store_total_orders) * 100 if store_total_orders > 0 else 0

            overall_metrics = {
                'store': store_name, 'orders': str(int(store_total_orders)),
                'units': str(int(store_total_units_picked)), 'uph': f"{overall_uph:.0f}",
                'inf': f"{overall_inf:.1f} %", 'lates': f"{overall_lates:.1f} %"
            }
            sorted_shoppers = sorted(shopper_stats, key=lambda x: float(x["inf"].replace("%", "").strip()))
            app_logger.info(f"Aggregated data for {len(sorted_shoppers)} shoppers in {store_name}.")
            return {"overall": overall_metrics, "shoppers": sorted_shoppers}
        except Exception as e:
            app_logger.warning(f"Attempt {attempt+1} failed for {store_name}: {e}", exc_info=True)
            if attempt == WORKER_RETRY_COUNT - 1:
                await _save_screenshot(getattr(ctx, 'pages', [None])[0], f"{store_name}_error")
        finally:
            if ctx: await ctx.close()
    app_logger.error(f"All attempts failed for {store_name}.")
    return None

async def post_to_webhook(url: str, payload: dict, store_name: str, hook_type: str):
    if not url: return
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    app_logger.error(f"{hook_type} webhook failed for {store_name}. Status: {resp.status}, Response: {err}")
                else:
                    app_logger.info(f"Successfully posted to {hook_type} webhook for {store_name}.")
    except Exception as e:
        app_logger.error(f"Error posting to {hook_type} webhook for {store_name}: {e}", exc_info=True)

async def post_store_report(data: dict):
    overall, shoppers = data.get("overall"), data.get("shoppers")
    store_name = overall.get("store", "Unknown Store")
    timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M")
    
    if not shoppers:
        summary_text = "No active shoppers found for this period."
        shopper_widgets = []
    else:
        summary_text = (f"• <b>UPH:</b> {_format_metric_with_emoji(overall.get('uph'), UPH_THRESHOLD, is_uph=True)}<br>"
                        f"• <b>Lates:</b> {_format_metric_with_emoji(overall.get('lates'), LATES_THRESHOLD)}<br>"
                        f"• <b>INF:</b> {_format_metric_with_emoji(overall.get('inf'), INF_THRESHOLD)}<br>"
                        f"• <b>Orders:</b> {overall.get('orders')}")
        shopper_widgets = [{
            "decoratedText": {
                "icon": {"knownIcon": "PERSON"}, "topLabel": f"<b>{s['name']}</b> ({s['orders']} Orders)",
                "text": f"{_format_metric_with_color(f'<b>UPH:</b> {s['uph']}', UPH_THRESHOLD, True)} | {_format_metric_with_color(f'<b>INF:</b> {s['inf']}', INF_THRESHOLD)} | {_format_metric_with_color(f'<b>Lates:</b> {s['lates']}', LATES_THRESHOLD)}"
            }} for s in shoppers]

    sections = [{"header": "Store-Wide Performance", "widgets": [{"textParagraph": {"text": summary_text}}]}]
    if shoppers:
        sections.append({"header": f"Per-Shopper Breakdown ({len(shoppers)})", "collapsible": True, "widgets": shopper_widgets})

    payload = {"cardsV2": [{"cardId": f"store-summary-{store_name.replace(' ', '-')}", "card": {
        "header": {"title": f"Amazon Metrics - {store_name}", "subtitle": timestamp, "imageUrl": "https://i.pinimg.com/originals/01/ca/da/01cada77a0a7d326d85b7969fe26a728.jpg", "imageType": "CIRCLE"},
        "sections": sections
    }}]}
    await post_to_webhook(CHAT_WEBHOOK_URL, payload, store_name, "per-store")

async def post_aggregate_summary(results: list):
    successful_results = [r for r in results if r and r.get("shoppers")]
    if not SUMMARY_CHAT_WEBHOOK_URL or not successful_results: return
    
    total_orders, total_units, fleet_pick_time_sec, fleet_weighted_lates, fleet_weighted_inf = 0, 0, 0, 0, 0
    store_widgets = []

    for res in successful_results:
        o = res["overall"]
        orders, units, uph = int(o.get('orders',0)), int(o.get('units',0)), float(o.get('uph',0))
        total_orders += orders
        total_units += units
        if uph > 0: fleet_pick_time_sec += (units / uph) * 3600
        fleet_weighted_lates += float(re.sub(r'[^\d.]', '', o.get('lates', '0'))) * orders
        fleet_weighted_inf += float(re.sub(r'[^\d.]', '', o.get('inf', '0'))) * units
        store_widgets.append({"decoratedText": {
            "icon": {"knownIcon": "STORE"}, "topLabel": f"<b>{o['store']}</b> ({orders} Orders)",
            "text": f"<b>UPH:</b> {o.get('uph')} | <b>Lates:</b> {o.get('lates')} | <b>INF:</b> {o.get('inf')}"
        }})

    fleet_uph = (total_units / (fleet_pick_time_sec / 3600)) if fleet_pick_time_sec > 0 else 0
    fleet_lates = (fleet_weighted_lates / total_orders) if total_orders > 0 else 0
    fleet_inf = (fleet_weighted_inf / total_units) if total_units > 0 else 0
    
    summary_text = (f"• <b>UPH:</b> {_format_metric_with_emoji(f'{fleet_uph:.0f}', UPH_THRESHOLD, True)}<br>"
                    f"• <b>Lates:</b> {_format_metric_with_emoji(f'{fleet_lates:.1f} %', LATES_THRESHOLD)}<br>"
                    f"• <b>INF:</b> {_format_metric_with_emoji(f'{fleet_inf:.1f} %', INF_THRESHOLD)}<br>"
                    f"• <b>Total Orders:</b> {total_orders}")

    payload = {"cardsV2": [{"cardId": "fleet-summary", "card": {
        "header": {"title": "Amazon Fleet Performance Summary", "subtitle": f"{datetime.now(LOCAL_TIMEZONE).strftime('%A %d %B, %H:%M')} | {len(successful_results)} stores", "imageUrl": "https://i.pinimg.com/originals/01/ca/da/01cada77a0a7d326d85b7969fe26a728.jpg", "imageType": "CIRCLE"},
        "sections": [
            {"header": "Fleet-Wide Performance (Weighted Avg)", "widgets": [{"textParagraph": {"text": summary_text}}]},
            {"header": "Per-Store Breakdown", "collapsible": True, "uncollapsibleWidgetsCount": 1, "widgets": store_widgets}
        ]
    }}]}
    await post_to_webhook(SUMMARY_CHAT_WEBHOOK_URL, payload, "Fleet", "summary")

async def main():
    global playwright, browser
    app_logger.info("Starting up in multi-store mode...")
    if not TARGET_STORES:
        app_logger.critical("`target_stores` is empty or not found in config.json. Aborting.")
        return
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=not DEBUG_MODE)

        login_required = True
        if ensure_storage_state():
            app_logger.info("Found existing storage_state; verifying session.")
            ctx = await browser.new_context(storage_state=json.load(open(STORAGE_STATE)))
            test_url = f"https://sellercentral.amazon.co.uk/snowdash?mons_sel_dir_mcid={TARGET_STORES[0]['merchant_id']}&mons_sel_mkid={TARGET_STORES[0]['marketplace_id']}"
            login_required = await check_if_login_needed(await ctx.new_page(), test_url)
            await ctx.close()

        if login_required:
            if not await prime_master_session():
                app_logger.critical("Could not establish a login session. Aborting.")
                return

        storage_state = json.load(open(STORAGE_STATE))
        all_results = []
        for store_info in TARGET_STORES:
            app_logger.info(f"===== Processing Store: {store_info.get('store_name', 'Unknown')} =====")
            scraped_data = await scrape_store_data(browser, store_info, storage_state)
            if scraped_data:
                all_results.append(scraped_data)
                await log_results(scraped_data)
                await post_store_report(scraped_data)
        
        if all_results:
            await post_aggregate_summary(all_results)
            app_logger.info(f"Run completed. Processed {len(all_results)}/{len(TARGET_STORES)} stores.")
        else:
            app_logger.error("Run failed: Could not retrieve data for any target stores.")

    except Exception as e:
        app_logger.critical(f"A critical error occurred in main execution: {e}", exc_info=True)
    finally:
        app_logger.info("Shutting down...")
        if browser: await browser.close()
        if playwright: await playwright.stop()
        app_logger.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())