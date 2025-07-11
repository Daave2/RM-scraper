# =======================================================================================
#         AMAZON SELLER CENTRAL SCRAPER (METRICS + TOP 5 INF ITEMS)
# =======================================================================================
# - Scrapes all stores first, then sends all notifications in a batch.
# - Includes highly robust login logic to handle multiple landing pages.
# - Posts a detailed, combined report for each store to a chat webhook.
# - Posts a final aggregate summary for all stores.
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
LOGIN_URL                = config.get('login_url', "https://sellercentral.amazon.co.uk/ap/signin")
CHAT_WEBHOOK_URL         = config.get('chat_webhook_url')
SUMMARY_CHAT_WEBHOOK_URL = config.get('summary_chat_webhook_url')
TARGET_STORES            = config.get('target_stores', []) 

# --- Thresholds & Formatting ---
EMOJI_GREEN_CHECK = "\u2705"
EMOJI_RED_CROSS   = "\u274C"
COLOR_GOOD        = "#2E8B57" 
COLOR_BAD         = "#CD5C5C"
UPH_THRESHOLD    = 80
LATES_THRESHOLD = 3.0
INF_THRESHOLD   = 2.0

# --- INF Scraper Specific Constants ---
SMALL_IMAGE_SIZE    = 300
QR_CODE_SIZE        = 60
WEBHOOK_DELAY_SECONDS = 1.0 # Delay between sending webhook messages to avoid rate limiting

# --- File Paths & Timeouts ---
JSON_LOG_FILE = os.path.join('output', 'submissions.jsonl')
STORAGE_STATE  = 'state.json'
OUTPUT_DIR     = 'output'
os.makedirs(OUTPUT_DIR, exist_ok=True)
PAGE_TIMEOUT       = 90_000
ACTION_TIMEOUT     = 45_000
WAIT_TIMEOUT       = 45_000

playwright = None
browser    = None
log_lock   = asyncio.Lock()


# =======================================================================================
#    AUTHENTICATION & SESSION MANAGEMENT (UPGRADED LOGIC)
# =======================================================================================
async def _save_screenshot(page: Page | None, prefix: str):
    if not page or page.is_closed(): return
    try:
        path = os.path.join(OUTPUT_DIR, f"{prefix}_{datetime.now(LOCAL_TIMEZONE).strftime('%Y%m%d_%H%M%S')}.png")
        await page.screenshot(path=path, full_page=True, timeout=15000)
        app_logger.info(f"Screenshot saved: {path}")
    except Exception as e:
        app_logger.error(f"Screenshot error: {e}")

def ensure_storage_state() -> bool:
    if not os.path.exists(STORAGE_STATE) or os.path.getsize(STORAGE_STATE) == 0: return False
    try:
        with open(STORAGE_STATE, 'r') as f: data = json.load(f)
        return isinstance(data, dict) and data.get("cookies")
    except (json.JSONDecodeError, IOError):
        return False

async def check_if_login_needed(page: Page, test_url: str) -> bool:
    try:
        await page.goto(test_url, timeout=PAGE_TIMEOUT, wait_until="load")
        if "signin" in page.url.lower() or "/ap/" in page.url:
            app_logger.info("Session invalid, login required.")
            return True
        await expect(page.locator("#dashboard-title-component-id")).to_be_visible(timeout=WAIT_TIMEOUT)
        app_logger.info("Existing session still valid.")
        return False
    except Exception:
        app_logger.warning("Error verifying session; assuming login required.")
        return True

async def perform_login(page: Page) -> bool:
    app_logger.info("Starting login flow")
    try:
        await page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT, wait_until="load")
        
        email_sel = "input#ap_email"
        continue_btn = 'button:has-text("Continue shopping")'
        continue_input = 'input[type="submit"][aria-labelledby="continue-announce"]'
        await page.wait_for_selector(f"{email_sel}, {continue_btn}, {continue_input}", timeout=WAIT_TIMEOUT)

        if await page.locator(continue_btn).is_visible():
            await page.locator(continue_btn).click()
        elif await page.locator(continue_input).is_visible():
            await page.locator(continue_input).click()
        
        await page.get_by_label("Email or mobile phone number").fill(config['login_email'])
        await page.get_by_label("Continue").click()
        await page.get_by_label("Password").fill(config['login_password'])
        await page.get_by_label("Sign in").click()

        otp_sel = 'input[id*="otp"]'
        # *** NEW LOGIC: Define all possible successful post-login pages ***
        acct_sel = 'h1:has-text("Select an account")'
        metrics_dash_sel = '#dashboard-title-component-id'
        inf_dash_sel = '#range-selector'
        shopper_perf_sel = 'h1:has-text("Shopper Performance")'
        
        possible_landing_pages = f"{otp_sel}, {acct_sel}, {metrics_dash_sel}, {inf_dash_sel}, {shopper_perf_sel}"
        await page.wait_for_selector(possible_landing_pages, timeout=WAIT_TIMEOUT)

        if await page.locator(otp_sel).is_visible():
            app_logger.info("OTP challenge detected.")
            code = pyotp.TOTP(config['otp_secret_key']).now()
            await page.locator(otp_sel).fill(code)
            await page.get_by_role("button", name="Sign in").click()
            await page.wait_for_selector(possible_landing_pages.replace(f"{otp_sel}, ", ""), timeout=WAIT_TIMEOUT)
        
        if await page.locator(acct_sel).is_visible():
            app_logger.warning("Landed on Account-picker page. Login successful.")
        elif await page.locator(shopper_perf_sel).is_visible():
            app_logger.warning("Landed on Shopper Performance page. Login successful.")
        elif await page.locator(metrics_dash_sel).is_visible():
             app_logger.info("Landed on Metrics dashboard. Login successful.")
        elif await page.locator(inf_dash_sel).is_visible():
             app_logger.info("Landed on INF dashboard. Login successful.")
        else:
            # This case should ideally not be reached if the selectors are correct
            app_logger.error("Landed on an unrecognized page after login.")
            raise TimeoutError("Could not confirm a successful login state.")

        app_logger.info("Login flow completed successfully.")
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
        
        app_logger.info("Finalizing session by visiting the first store's metrics dashboard.")
        first_store_url = f"https://sellercentral.amazon.co.uk/snowdash?mons_sel_dir_mcid={TARGET_STORES[0]['merchant_id']}&mons_sel_mkid={TARGET_STORES[0]['marketplace_id']}"
        await page.goto(first_store_url, timeout=PAGE_TIMEOUT, wait_until="load")
        await expect(page.locator("#dashboard-title-component-id")).to_be_visible(timeout=WAIT_TIMEOUT)

        await ctx.storage_state(path=STORAGE_STATE)
        app_logger.info("Saved new session state.")
        return True
    finally:
        await ctx.close()

# =======================================================================================
#                       CORE SCRAPING LOGIC
# =======================================================================================
def _format_metric_with_emoji(value_str: str, threshold: float, is_uph: bool = False) -> str:
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        return f"{value_str} {EMOJI_GREEN_CHECK if is_good else EMOJI_RED_CROSS}"
    except (ValueError, TypeError): return value_str
        
def _format_metric_with_color(value_str: str, threshold: float, is_uph: bool = False) -> str:
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        return f'<font color="{COLOR_GOOD if is_good else COLOR_BAD}">{value_str}</font>'
    except (ValueError, TypeError): return value_str

async def scrape_store_metrics(page: Page, store_info: dict) -> dict | None:
    store_name = store_info['store_name']
    app_logger.info(f"Starting METRICS data collection for '{store_name}'")
    try:
        dash_url = (f"https://sellercentral.amazon.co.uk/snowdash?mons_sel_dir_mcid={store_info['merchant_id']}&mons_sel_mkid={store_info['marketplace_id']}")
        await page.goto(dash_url, timeout=PAGE_TIMEOUT)
        await expect(page.get_by_role("button", name="Refresh")).to_be_visible(timeout=WAIT_TIMEOUT)
        await page.locator("#content span:has-text('Customised')").nth(0).click(timeout=ACTION_TIMEOUT)
        await expect(page.locator("kat-date-range-picker")).to_be_visible(timeout=WAIT_TIMEOUT)
        now = datetime.now(LOCAL_TIMEZONE).strftime("%m/%d/%Y")
        date_inputs = page.locator('kat-date-range-picker input[type="text"]')
        await date_inputs.nth(0).fill(now); await date_inputs.nth(1).fill(now)
        async with page.expect_response(lambda r: "/api/metrics" in r.url, timeout=40000) as response_info:
            await page.get_by_role("button", name="Apply").click(timeout=ACTION_TIMEOUT)
        api_data = await (await response_info.value).json()
        app_logger.info(f"Received METRICS API response for {store_name}.")
        
        shopper_stats, store_totals = [], {'units':0, 'time':0, 'orders':0, 'req_units':0, 'inf_items':0, 'lates':0}
        for entry in api_data:
            m = entry.get("metrics", {}); name = entry.get("shopperName")
            if entry.get("type") == "MASTER" and name and name != "SHOPPER_NAME_NOT_FOUND":
                orders = m.get("OrdersShopped_V2", 0)
                if orders == 0: continue
                units = m.get("PickedUnits_V2", 0); time_sec = m.get("PickTimeInSec_V2", 0)
                shopper_stats.append({"name":name, "uph":f"{(units / (time_sec / 3600)) if time_sec > 0 else 0:.0f}", "inf":f"{m.get('ItemNotFoundRate_V2',0):.1f} %", "lates":f"{m.get('LatePicksRate',0):.1f} %", "orders":int(orders)})
                store_totals['units'] += units; store_totals['time'] += time_sec; store_totals['orders'] += orders
                req_units = m.get("RequestedQuantity_V2", 0); store_totals['req_units'] += req_units
                store_totals['inf_items'] += req_units * (m.get("ItemNotFoundRate_V2", 0) / 100.0)
                store_totals['lates'] += orders * (m.get("LatePicksRate", 0) / 100.0)
        
        if not shopper_stats:
            app_logger.warning(f"No active shoppers found for {store_name}.")
            return {"overall": {'store': store_name}, "shoppers": []}
        
        overall_uph = (store_totals['units']/(store_totals['time']/3600)) if store_totals['time']>0 else 0
        overall_inf = (store_totals['inf_items']/store_totals['req_units'])*100 if store_totals['req_units']>0 else 0
        overall_lates = (store_totals['lates']/store_totals['orders'])*100 if store_totals['orders']>0 else 0
        overall = {'store':store_name, 'orders':str(int(store_totals['orders'])), 'units':str(int(store_totals['units'])), 'uph':f"{overall_uph:.0f}", 'inf':f"{overall_inf:.1f} %", 'lates':f"{overall_lates:.1f} %"}
        shoppers = sorted(shopper_stats, key=lambda x: float(x["inf"].replace("%","").strip()))
        return {"overall": overall, "shoppers": shoppers}
    except Exception as e:
        app_logger.error(f"Error scraping metrics for {store_name}: {e}", exc_info=True)
        await _save_screenshot(page, f"{store_name}_metrics_error")
        return None

async def scrape_inf_data(page: Page, store_info: dict) -> list[dict] | None:
    store_name = store_info["store_name"]
    app_logger.info(f"Starting INF data collection for '{store_name}'")
    try:
        url = "https://sellercentral.amazon.co.uk/snow-inventory/inventoryinsights/ref=xx_infr_dnav_xx"
        await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        await expect(page.locator("#range-selector")).to_be_visible(timeout=WAIT_TIMEOUT)
        
        table_sel = "table.imp-table tbody"
        try:
            await expect(page.locator(f"{table_sel} tr").first).to_be_visible(timeout=20000)
        except TimeoutError:
            app_logger.info(f"No INF data rows found for '{store_name}'; returning empty list.")
            return []
        
        first_row_before_sort = await page.locator(f"{table_sel} tr").first.text_content()
        app_logger.info(f"Sorting table by 'INF Units' for '{store_name}'")
        await page.locator("#sort-3").click()
        try:
            await page.wait_for_function(expression="(args) => { const [selector, initialText] = args; const firstRow = document.querySelector(selector); return firstRow && firstRow.textContent !== initialText; }", arg=[f"{table_sel} tr", first_row_before_sort], timeout=20000)
            app_logger.info("Table sort confirmed by DOM change.")
        except TimeoutError:
            app_logger.warning("Table content did not change after sort click. Proceeding with current data (might be pre-sorted or single-page).")

        rows = await page.locator(f"{table_sel} tr").all()
        items = []
        for r in rows[:5]:
            cells = r.locator("td")
            thumb = await cells.nth(0).locator("img").get_attribute("src") or ""
            items.append({"image_url": re.sub(r"\._SS\d+_\.", f"._SS{SMALL_IMAGE_SIZE}_.", thumb), "sku": await cells.nth(1).locator("span").inner_text(), "product_name": await cells.nth(2).locator("a span").inner_text(), "inf_units": await cells.nth(3).locator("span").inner_text(), "orders_impacted": await cells.nth(4).locator("span").inner_text(), "inf_pct": await cells.nth(8).locator("span").inner_text()})
        app_logger.info(f"Scraped top {len(items)} INF items for '{store_name}'")
        return items
    except Exception as e:
        app_logger.error(f"Error scraping INF data for {store_name}: {e}", exc_info=True)
        await _save_screenshot(page, f"{store_name}_inf_error")
        return None

# =======================================================================================
#                       NOTIFICATIONS
# =======================================================================================
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
    overall, shoppers, inf_items = data.get("overall",{}), data.get("shoppers",[]), data.get("inf_items", [])
    full_store_name = overall.get("store", "Unknown Store")
    timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M")
    short_store_name = full_store_name.split(' - ')[-1] if ' - ' in full_store_name else full_store_name
    
    sections = []
    if shoppers:
        summary_text = (f"• <b>UPH:</b> {_format_metric_with_emoji(overall.get('uph'), UPH_THRESHOLD, is_uph=True)}<br>"f"• <b>Lates:</b> {_format_metric_with_emoji(overall.get('lates'), LATES_THRESHOLD)}<br>"f"• <b>INF:</b> {_format_metric_with_emoji(overall.get('inf'), INF_THRESHOLD)}<br>"f"• <b>Orders:</b> {overall.get('orders')}")
        sections.append({"header": "Store-Wide Performance", "widgets": [{"textParagraph": {"text": summary_text}}]})
    else:
        sections.append({"header": "Store-Wide Performance", "widgets": [{"textParagraph": {"text": "No active shoppers found for this period."}}]})

    if shoppers:
        shopper_widgets = []
        for s in shoppers:
            uph = _format_metric_with_color(f"<b>UPH:</b> {s['uph']}", UPH_THRESHOLD, True); inf = _format_metric_with_color(f"<b>INF:</b> {s['inf']}", INF_THRESHOLD); lates = _format_metric_with_color(f"<b>Lates:</b> {s['lates']}", LATES_THRESHOLD)
            shopper_widgets.append({"decoratedText": {"icon":{"knownIcon":"PERSON"}, "topLabel":f"<b>{s['name']}</b> ({s['orders']} Orders)", "text":f"{uph} | {inf} | {lates}"}})
        sections.append({"header":f"Per-Shopper Breakdown ({len(shoppers)})", "collapsible":True, "widgets":shopper_widgets})

    if inf_items:
        inf_widgets = [{"divider": {}}]
        for it in inf_items:
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size={QR_CODE_SIZE}x{QR_CODE_SIZE}&data={urllib.parse.quote(it['sku'])}"
            left_col = {"horizontalSizeStyle":"FILL_MINIMUM_SPACE", "widgets":[{"image":{"imageUrl":qr_url}}]}
            right_col = {"widgets":[{"textParagraph":{"text":f"<b>{it['product_name']}</b><br><b>SKU:</b> {it['sku']}<br><b>INF Units:</b> {it['inf_units']} ({it['inf_pct']}) | <b>Orders:</b> {it['orders_impacted']}"}}, {"image":{"imageUrl":it["image_url"]}}]}
            inf_widgets.extend([{"columns": {"columnItems": [left_col, right_col]}}, {"divider": {}}])
        sections.append({"header": f"Top {len(inf_items)} INF Items", "collapsible": True, "uncollapsibleWidgetsCount": 1, "widgets": inf_widgets})

    payload = {"cardsV2": [{"cardId": f"store-report-{full_store_name.replace(' ', '-')}", "card": {"header": {"title": short_store_name, "subtitle": timestamp, "imageUrl": "https://i.pinimg.com/originals/01/ca/da/01cada77a0a7d326d85b7969fe26a728.jpg", "imageType": "CIRCLE"}, "sections": sections}}]}
    await post_to_webhook(CHAT_WEBHOOK_URL, payload, full_store_name, "per-store")

async def post_aggregate_summary(results: list):
    successful_results = [r for r in results if r.get("overall", {}).get("store")]
    if not SUMMARY_CHAT_WEBHOOK_URL or not successful_results: return
    
    total_orders, total_units, fleet_pick_time_sec, fleet_weighted_lates, fleet_weighted_inf = 0,0,0,0,0
    store_widgets = []
    for idx, res in enumerate(successful_results):
        o = res["overall"]; inf_list = res.get("inf_items", [])
        orders, units, uph = int(o.get('orders',0)), int(o.get('units',0)), float(o.get('uph',0))
        total_orders += orders; total_units += units
        if uph > 0: fleet_pick_time_sec += (units / uph) * 3600
        fleet_weighted_lates += float(re.sub(r'[^\d.]','',o.get('lates','0'))) * orders
        fleet_weighted_inf += float(re.sub(r'[^\d.]','',o.get('inf','0'))) * units
        
        uph_f = _format_metric_with_color(f"<b>UPH:</b> {o.get('uph')}", UPH_THRESHOLD, True); lates_f = _format_metric_with_color(f"<b>Lates:</b> {o.get('lates')}", LATES_THRESHOLD); inf_f = _format_metric_with_color(f"<b>INF:</b> {o.get('inf')}", INF_THRESHOLD)
        metrics_text = f"{uph_f} | {lates_f} | {inf_f}"
        store_widgets.append({"decoratedText": {"icon":{"knownIcon":"STORE"}, "topLabel":f"<b>{o['store']}</b> ({orders} Orders)", "text":metrics_text}})
        if inf_list: store_widgets.append({"textParagraph": {"text": f"<i>Top INF: {inf_list[0]['product_name']}</i>"}})
        if idx < len(successful_results) - 1: store_widgets.append({"divider": {}})

    fleet_uph = (total_units/(fleet_pick_time_sec/3600)) if fleet_pick_time_sec > 0 else 0
    fleet_lates = (fleet_weighted_lates/total_orders) if total_orders > 0 else 0
    fleet_inf = (fleet_weighted_inf/total_units) if total_units > 0 else 0
    summary_text = (f"• <b>UPH:</b> {_format_metric_with_emoji(f'{fleet_uph:.0f}', UPH_THRESHOLD, True)}<br>"f"• <b>Lates:</b> {_format_metric_with_emoji(f'{fleet_lates:.1f} %', LATES_THRESHOLD)}<br>"f"• <b>INF:</b> {_format_metric_with_emoji(f'{fleet_inf:.1f} %', INF_THRESHOLD)}<br>"f"• <b>Total Orders:</b> {total_orders}")
    payload = {"cardsV2": [{"cardId": "fleet-summary", "card": {"header": {"title": "Amazon North West Summary", "subtitle":f"{datetime.now(LOCAL_TIMEZONE).strftime('%A %d %B, %H:%M')} | {len(successful_results)} stores", "imageUrl":"https://i.pinimg.com/originals/01/ca/da/01cada77a0a7d326d85b7969fe26a728.jpg", "imageType":"CIRCLE"}, "sections": [{"header": "Fleet-Wide Performance (Weighted Avg)", "widgets": [{"textParagraph": {"text": summary_text}}]}, {"header": "Per-Store Breakdown", "collapsible":True, "uncollapsibleWidgetsCount":len(store_widgets), "widgets":store_widgets}]}}]}
    await post_to_webhook(SUMMARY_CHAT_WEBHOOK_URL, payload, "Fleet", "summary")

async def log_results(data: dict):
    async with log_lock:
        log_entry = {'timestamp':datetime.now(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'), **data}
        try:
            async with aiofiles.open(JSON_LOG_FILE, 'a', encoding='utf-8') as f:
                await f.write(json.dumps(log_entry) + '\n')
        except IOError as e: app_logger.error(f"Error writing to JSON log file {JSON_LOG_FILE}: {e}")

# =======================================================================================
#                       MAIN EXECUTION
# =======================================================================================
async def main():
    global playwright, browser
    app_logger.info("Starting up unified scraper (Metrics + INF)...")
    if not TARGET_STORES:
        app_logger.critical("`target_stores` is empty or not found in config.json. Aborting.")
        return
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=not DEBUG_MODE)

        login_required = True
        if ensure_storage_state():
            app_logger.info("Found existing storage_state; verifying session.")
            ctx_check = await browser.new_context(storage_state=json.load(open(STORAGE_STATE)))
            test_url = f"https://sellercentral.amazon.co.uk/snowdash?mons_sel_dir_mcid={TARGET_STORES[0]['merchant_id']}&mons_sel_mkid={TARGET_STORES[0]['marketplace_id']}"
            login_required = await check_if_login_needed(await ctx_check.new_page(), test_url)
            await ctx_check.close()
        
        if login_required:
            if not await prime_master_session():
                app_logger.critical("Could not establish a login session. Aborting.")
                return

        storage_state = json.load(open(STORAGE_STATE))
        all_results = []
        for store_info in TARGET_STORES:
            store_name = store_info.get('store_name', 'Unknown')
            app_logger.info(f"===== Processing Store: {store_name} =====")
            ctx = None
            try:
                ctx = await browser.new_context(storage_state=storage_state)
                page = await ctx.new_page()
                
                metrics_data = await scrape_store_metrics(page, store_info)
                inf_items = await scrape_inf_data(page, store_info)

                if metrics_data:
                    combined_data = {**metrics_data, "inf_items": inf_items if inf_items is not None else []}
                    all_results.append(combined_data)
                    await log_results(combined_data)
                else:
                    app_logger.error(f"Failed to retrieve any metrics for {store_name}. Skipping for this store.")
            
            except Exception as e:
                app_logger.error(f"An unexpected error occurred while processing {store_name}: {e}", exc_info=True)
            finally:
                if ctx: await ctx.close()
        
        if all_results:
            app_logger.info(f"Scraping complete. Sending {len(all_results)} store reports...")
            for result in all_results:
                await post_store_report(result)
                app_logger.info(f"Waiting {WEBHOOK_DELAY_SECONDS}s before next webhook post...")
                await asyncio.sleep(WEBHOOK_DELAY_SECONDS)
            
            app_logger.info("Sending aggregate summary report...")
            await post_aggregate_summary(all_results)
            app_logger.info(f"Run completed. Processed {len(all_results)}/{len(TARGET_STORES)} stores successfully.")
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