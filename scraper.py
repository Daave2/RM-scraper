from __future__ import annotations
import asyncio
import json
from datetime import datetime

from playwright.async_api import async_playwright
import aiofiles

from src.settings import (
    app_logger,
    DEBUG_MODE,
    TARGET_STORES,
    STORAGE_STATE,
    JSON_LOG_FILE,
    WEBHOOK_DELAY_SECONDS,
    SCRAPE_RETRY_ATTEMPTS,
    SCRAPE_RETRY_DELAY,
    LOCAL_TIMEZONE,
)
from src.utils import ensure_storage_state
from src.auth import check_if_login_needed, prime_master_session
from src.metrics import scrape_store_metrics, scrape_inf_data
from src.notifications import post_store_report, post_aggregate_summary

playwright = None
browser = None
log_lock = asyncio.Lock()

async def run_with_retries(func, *args, max_attempts=SCRAPE_RETRY_ATTEMPTS, attempt_delay=SCRAPE_RETRY_DELAY, **kwargs):
    for attempt in range(1, max_attempts + 1):
        try:
            result = await func(*args, **kwargs)
            if result is not None:
                return result
            raise ValueError("result was None")
        except Exception as e:
            if attempt == max_attempts:
                app_logger.error(f"{func.__name__} failed after {max_attempts} attempts: {e}")
                return None
            app_logger.warning(f"{func.__name__} attempt {attempt} failed: {e}. Retrying in {attempt_delay}s...")
            await asyncio.sleep(attempt_delay)

async def log_results(data: dict):
    async with log_lock:
        log_entry = {
            'timestamp': datetime.now(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
            **data,
        }
        try:
            async with aiofiles.open(JSON_LOG_FILE, 'a', encoding='utf-8') as f:
                await f.write(json.dumps(log_entry) + '\n')
        except IOError as e:
            app_logger.error(f"Error writing to JSON log file {JSON_LOG_FILE}: {e}")

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
            test_url = (
                f"https://sellercentral.amazon.co.uk/snowdash?mons_sel_dir_mcid={TARGET_STORES[0]['merchant_id']}"
                f"&mons_sel_mkid={TARGET_STORES[0]['marketplace_id']}"
            )
            login_required = await check_if_login_needed(await ctx_check.new_page(), test_url)
            await ctx_check.close()

        if login_required:
            if not await prime_master_session(browser):
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

                metrics_data = await run_with_retries(scrape_store_metrics, page, store_info)
                inf_items = await run_with_retries(scrape_inf_data, page, store_info)

                if metrics_data:
                    combined_data = {**metrics_data, 'inf_items': inf_items if inf_items is not None else []}
                    all_results.append(combined_data)
                    await log_results(combined_data)
                else:
                    app_logger.error(f"Failed to retrieve any metrics for {store_name}. Skipping for this store.")

            except Exception as e:
                app_logger.error(f"An unexpected error occurred while processing {store_name}: {e}", exc_info=True)
            finally:
                if ctx:
                    await ctx.close()

        if all_results:
            app_logger.info(f"Scraping complete. Sending {len(all_results)} store reports...")
            for result in all_results:
                await post_store_report(result)
                app_logger.info(f"Waiting {WEBHOOK_DELAY_SECONDS}s before next webhook post...")
                await asyncio.sleep(WEBHOOK_DELAY_SECONDS)

            app_logger.info("Sending aggregate summary report...")
            await post_aggregate_summary(all_results)
            app_logger.info(
                f"Run completed. Processed {len(all_results)}/{len(TARGET_STORES)} stores successfully."
            )
        else:
            app_logger.error("Run failed: Could not retrieve data for any target stores.")

    except Exception as e:
        app_logger.critical(f"A critical error occurred in main execution: {e}", exc_info=True)
    finally:
        app_logger.info("Shutting down...")
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
        app_logger.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
