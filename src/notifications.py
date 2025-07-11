from __future__ import annotations
import re
import urllib.parse
from datetime import datetime
import aiohttp
import ssl
import certifi
from .settings import (
    CHAT_WEBHOOK_URL,
    SUMMARY_CHAT_WEBHOOK_URL,
    UPH_THRESHOLD,
    LATES_THRESHOLD,
    INF_THRESHOLD,
    QR_CODE_SIZE,
    WEBHOOK_DELAY_SECONDS,
    COLOR_GOOD,
    COLOR_BAD,
    EMOJI_GREEN_CHECK,
    EMOJI_RED_CROSS,
    LOCAL_TIMEZONE,
    app_logger,
)
from .utils import save_screenshot  # not used maybe

async def post_to_webhook(url: str, payload: dict, store_name: str, hook_type: str):
    if not url:
        return
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


def _format_metric_with_emoji(value_str: str, threshold: float, is_uph: bool = False) -> str:
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        return f"{value_str} {EMOJI_GREEN_CHECK if is_good else EMOJI_RED_CROSS}"
    except (ValueError, TypeError):
        return value_str


def _format_metric_with_color(value_str: str, threshold: float, is_uph: bool = False) -> str:
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        return f'<font color="{COLOR_GOOD if is_good else COLOR_BAD}">{value_str}</font>'
    except (ValueError, TypeError):
        return value_str

async def post_store_report(data: dict):
    overall = data.get('overall', {})
    shoppers = data.get('shoppers', [])
    inf_items = data.get('inf_items', [])
    full_store_name = overall.get('store', 'Unknown Store')
    timestamp = datetime.now(LOCAL_TIMEZONE).strftime('%A %d %B, %H:%M')
    short_store_name = full_store_name.split(' - ')[-1] if ' - ' in full_store_name else full_store_name

    sections = []
    if shoppers:
        summary_text = (
            f"• <b>UPH:</b> {_format_metric_with_emoji(overall.get('uph'), UPH_THRESHOLD, is_uph=True)}<br>"
            f"• <b>Lates:</b> {_format_metric_with_emoji(overall.get('lates'), LATES_THRESHOLD)}<br>"
            f"• <b>INF:</b> {_format_metric_with_emoji(overall.get('inf'), INF_THRESHOLD)}<br>"
            f"• <b>Orders:</b> {overall.get('orders')}"
        )
        sections.append({'header': 'Store-Wide Performance', 'widgets': [{'textParagraph': {'text': summary_text}}]})
    else:
        sections.append({'header': 'Store-Wide Performance', 'widgets': [{'textParagraph': {'text': 'No active shoppers found for this period.'}}]})

    if shoppers:
        shopper_widgets = []
        for s in shoppers:
            uph = _format_metric_with_color(f"<b>UPH:</b> {s['uph']}", UPH_THRESHOLD, True)
            inf = _format_metric_with_color(f"<b>INF:</b> {s['inf']}", INF_THRESHOLD)
            lates = _format_metric_with_color(f"<b>Lates:</b> {s['lates']}", LATES_THRESHOLD)
            shopper_widgets.append({'decoratedText': {'icon': {'knownIcon': 'PERSON'}, 'topLabel': f"<b>{s['name']}</b> ({s['orders']} Orders)", 'text': f"{uph} | {inf} | {lates}"}})
        sections.append({'header': f"Per-Shopper Breakdown ({len(shoppers)})", 'collapsible': True, 'widgets': shopper_widgets})

    if inf_items:
        inf_widgets = [{'divider': {}}]
        for it in inf_items:
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size={QR_CODE_SIZE}x{QR_CODE_SIZE}&data={urllib.parse.quote(it['sku'])}"
            left_col = {'horizontalSizeStyle': 'FILL_MINIMUM_SPACE', 'widgets': [{'image': {'imageUrl': qr_url}}]}
            right_col = {'widgets': [
                {'textParagraph': {'text': f"<b>{it['product_name']}</b><br><b>SKU:</b> {it['sku']}<br><b>INF Units:</b> {it['inf_units']} ({it['inf_pct']}) | <b>Orders:</b> {it['orders_impacted']}"}},
                {'image': {'imageUrl': it['image_url']}}
            ]}
            inf_widgets.extend([{'columns': {'columnItems': [left_col, right_col]}}, {'divider': {}}])
        sections.append({'header': f"Top {len(inf_items)} INF Items", 'collapsible': True, 'uncollapsibleWidgetsCount': 1, 'widgets': inf_widgets})

    payload = {
        'cardsV2': [{
            'cardId': f"store-report-{full_store_name.replace(' ', '-')}",
            'card': {
                'header': {
                    'title': short_store_name,
                    'subtitle': timestamp,
                    'imageUrl': 'https://i.pinimg.com/originals/01/ca/da/01cada77a0a7d326d85b7969fe26a728.jpg',
                    'imageType': 'CIRCLE'
                },
                'sections': sections
            }
        }]
    }
    await post_to_webhook(CHAT_WEBHOOK_URL, payload, full_store_name, 'per-store')

async def post_aggregate_summary(results: list):
    successful_results = [r for r in results if r.get('overall', {}).get('store')]
    if not SUMMARY_CHAT_WEBHOOK_URL or not successful_results:
        return

    total_orders = 0
    total_units = 0
    fleet_pick_time_sec = 0
    fleet_weighted_lates = 0
    fleet_weighted_inf = 0
    store_widgets = []
    for idx, res in enumerate(successful_results):
        o = res['overall']
        inf_list = res.get('inf_items', [])
        orders = int(o.get('orders', 0))
        units = int(o.get('units', 0))
        uph = float(o.get('uph', 0))
        total_orders += orders
        total_units += units
        if uph > 0:
            fleet_pick_time_sec += (units / uph) * 3600
        fleet_weighted_lates += float(re.sub(r'[^\d.]', '', o.get('lates', '0'))) * orders
        fleet_weighted_inf += float(re.sub(r'[^\d.]', '', o.get('inf', '0'))) * units

        uph_f = _format_metric_with_color(f"<b>UPH:</b> {o.get('uph')}", UPH_THRESHOLD, True)
        lates_f = _format_metric_with_color(f"<b>Lates:</b> {o.get('lates')}", LATES_THRESHOLD)
        inf_f = _format_metric_with_color(f"<b>INF:</b> {o.get('inf')}", INF_THRESHOLD)
        metrics_text = f"{uph_f} | {lates_f} | {inf_f}"
        store_widgets.append({'decoratedText': {'icon': {'knownIcon': 'STORE'}, 'topLabel': f"<b>{o['store']}</b> ({orders} Orders)", 'text': metrics_text}})
        if inf_list:
            store_widgets.append({'textParagraph': {'text': f"<i>Top INF: {inf_list[0]['product_name']}</i>"}})
        if idx < len(successful_results) - 1:
            store_widgets.append({'divider': {}})

    fleet_uph = (total_units / (fleet_pick_time_sec / 3600)) if fleet_pick_time_sec > 0 else 0
    fleet_lates = (fleet_weighted_lates / total_orders) if total_orders > 0 else 0
    fleet_inf = (fleet_weighted_inf / total_units) if total_units > 0 else 0
    summary_text = (
        f"• <b>UPH:</b> {_format_metric_with_emoji(f'{fleet_uph:.0f}', UPH_THRESHOLD, True)}<br>"
        f"• <b>Lates:</b> {_format_metric_with_emoji(f'{fleet_lates:.1f} %', LATES_THRESHOLD)}<br>"
        f"• <b>INF:</b> {_format_metric_with_emoji(f'{fleet_inf:.1f} %', INF_THRESHOLD)}<br>"
        f"• <b>Total Orders:</b> {total_orders}"
    )
    payload = {
        'cardsV2': [{
            'cardId': 'fleet-summary',
            'card': {
                'header': {
                    'title': 'Amazon North West Summary',
                    'subtitle': f"{datetime.now(LOCAL_TIMEZONE).strftime('%A %d %B, %H:%M')} | {len(successful_results)} stores",
                    'imageUrl': 'https://i.pinimg.com/originals/01/ca/da/01cada77a0a7d326d85b7969fe26a728.jpg',
                    'imageType': 'CIRCLE'
                },
                'sections': [
                    {'header': 'Fleet-Wide Performance (Weighted Avg)', 'widgets': [{'textParagraph': {'text': summary_text}}]},
                    {'header': 'Per-Store Breakdown', 'collapsible': True, 'uncollapsibleWidgetsCount': len(store_widgets), 'widgets': store_widgets}
                ]
            }
        }]
    }
    await post_to_webhook(SUMMARY_CHAT_WEBHOOK_URL, payload, 'Fleet', 'summary')

