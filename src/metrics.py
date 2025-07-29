from __future__ import annotations
import re
from datetime import datetime
from playwright.async_api import Page, TimeoutError, expect
from .settings import (
    PAGE_TIMEOUT,
    WAIT_TIMEOUT,
    ACTION_TIMEOUT,
    SMALL_IMAGE_SIZE,
    LOCAL_TIMEZONE,
    app_logger,
)
from .utils import save_screenshot

async def scrape_store_metrics(page: Page, store_info: dict) -> dict | None:
    store_name = store_info['store_name']
    app_logger.info(f"Starting METRICS data collection for '{store_name}'")
    try:
        dash_url = (
            f"https://sellercentral.amazon.co.uk/snowdash?mons_sel_dir_mcid={store_info['merchant_id']}"
            f"&mons_sel_mkid={store_info['marketplace_id']}"
        )
        await page.goto(dash_url, timeout=PAGE_TIMEOUT)
        await expect(page.get_by_role("button", name="Refresh")).to_be_visible(timeout=WAIT_TIMEOUT)

        customise_btn = page.get_by_role(
            "button", name=re.compile("Customise", re.I)
        )
        await expect(customise_btn).to_be_visible(timeout=WAIT_TIMEOUT)
        await expect(customise_btn).to_be_enabled(timeout=WAIT_TIMEOUT)
        await customise_btn.click(timeout=ACTION_TIMEOUT)
        await expect(page.locator("kat-date-range-picker")).to_be_visible(timeout=WAIT_TIMEOUT)
        now = datetime.now(LOCAL_TIMEZONE).strftime("%m/%d/%Y")
        date_inputs = page.locator('kat-date-range-picker input[type="text"]')
        await date_inputs.nth(0).fill(now)
        await date_inputs.nth(1).fill(now)
        async with page.expect_response(lambda r: "/api/metrics" in r.url, timeout=40000) as response_info:
            await page.get_by_role("button", name="Apply").click(timeout=ACTION_TIMEOUT)
        api_data = await (await response_info.value).json()
        app_logger.info(f"Received METRICS API response for {store_name}.")

        shopper_stats = []
        store_totals = {'units':0, 'time':0, 'orders':0, 'req_units':0, 'inf_items':0, 'lates':0}
        for entry in api_data:
            m = entry.get('metrics', {})
            name = entry.get('shopperName')
            if entry.get('type') == 'MASTER' and name and name != 'SHOPPER_NAME_NOT_FOUND':
                orders = m.get('OrdersShopped_V2', 0)
                if orders == 0:
                    continue
                units = m.get('PickedUnits_V2', 0)
                time_sec = m.get('PickTimeInSec_V2', 0)
                shopper_stats.append({
                    'name': name,
                    'uph': f"{(units / (time_sec / 3600)) if time_sec > 0 else 0:.0f}",
                    'inf': f"{m.get('ItemNotFoundRate_V2',0):.1f} %",
                    'lates': f"{m.get('LatePicksRate',0):.1f} %",
                    'orders': int(orders)
                })
                store_totals['units'] += units
                store_totals['time'] += time_sec
                store_totals['orders'] += orders
                req_units = m.get('RequestedQuantity_V2', 0)
                store_totals['req_units'] += req_units
                store_totals['inf_items'] += req_units * (m.get('ItemNotFoundRate_V2', 0) / 100.0)
                store_totals['lates'] += orders * (m.get('LatePicksRate', 0) / 100.0)

        if not shopper_stats:
            app_logger.warning(f"No active shoppers found for {store_name}.")
            return {'overall': {'store': store_name}, 'shoppers': []}

        overall_uph = (store_totals['units']/(store_totals['time']/3600)) if store_totals['time']>0 else 0
        overall_inf = (store_totals['inf_items']/store_totals['req_units'])*100 if store_totals['req_units']>0 else 0
        overall_lates = (store_totals['lates']/store_totals['orders'])*100 if store_totals['orders']>0 else 0
        overall = {
            'store': store_name,
            'orders': str(int(store_totals['orders'])),
            'units': str(int(store_totals['units'])),
            'uph': f"{overall_uph:.0f}",
            'inf': f"{overall_inf:.1f} %",
            'lates': f"{overall_lates:.1f} %",
        }
        shoppers = sorted(shopper_stats, key=lambda x: float(x['inf'].replace('%','').strip()))
        return {'overall': overall, 'shoppers': shoppers}
    except Exception as e:
        app_logger.error(f"Error scraping metrics for {store_name}: {e}", exc_info=True)
        await save_screenshot(page, f"{store_name}_metrics_error")
        return None

async def scrape_inf_data(page: Page, store_info: dict) -> list[dict] | None:
    store_name = store_info['store_name']
    app_logger.info(f"Starting INF data collection for '{store_name}'")
    try:
        url = 'https://sellercentral.amazon.co.uk/snow-inventory/inventoryinsights/ref=xx_infr_dnav_xx'
        await page.goto(url, timeout=PAGE_TIMEOUT, wait_until='domcontentloaded')
        await expect(page.locator('#range-selector')).to_be_visible(timeout=WAIT_TIMEOUT)

        table_sel = 'table.imp-table tbody'
        try:
            await expect(page.locator(f"{table_sel} tr").first).to_be_visible(timeout=20000)
        except TimeoutError:
            app_logger.info(f"No INF data rows found for '{store_name}'; returning empty list.")
            return []

        first_row_before_sort = await page.locator(f"{table_sel} tr").first.text_content()
        app_logger.info(f"Sorting table by 'INF Units' for '{store_name}'")
        await page.locator('#sort-3').click()
        try:
            await page.wait_for_function(
                expression='(args) => { const [selector, initialText] = args; const firstRow = document.querySelector(selector); return firstRow && firstRow.textContent !== initialText; }',
                arg=[f"{table_sel} tr", first_row_before_sort],
                timeout=20000,
            )
            app_logger.info('Table sort confirmed by DOM change.')
        except TimeoutError:
            app_logger.warning('Table content did not change after sort click. Proceeding with current data (might be pre-sorted or single-page).')

        rows = await page.locator(f"{table_sel} tr").all()
        items = []
        for r in rows[:5]:
            cells = r.locator('td')
            thumb = await cells.nth(0).locator('img').get_attribute('src') or ''
            items.append({
                'image_url': re.sub(r"\._SS\d+_\.", f"._SS{SMALL_IMAGE_SIZE}_.", thumb),
                'sku': await cells.nth(1).locator('span').inner_text(),
                'product_name': await cells.nth(2).locator('a span').inner_text(),
                'inf_units': await cells.nth(3).locator('span').inner_text(),
                'orders_impacted': await cells.nth(4).locator('span').inner_text(),
                'inf_pct': await cells.nth(8).locator('span').inner_text(),
            })
        app_logger.info(f"Scraped top {len(items)} INF items for '{store_name}'")
        return items
    except Exception as e:
        app_logger.error(f"Error scraping INF data for {store_name}: {e}", exc_info=True)
        await save_screenshot(page, f"{store_name}_inf_error")
        return None
