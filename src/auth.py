from __future__ import annotations
import pyotp
from playwright.async_api import Page, Browser, expect
from .settings import (
    LOGIN_URL,
    PAGE_TIMEOUT,
    WAIT_TIMEOUT,
    STORAGE_STATE,
    TARGET_STORES,
    app_logger,
    DEBUG_MODE,
    config,
)
from .utils import save_screenshot

async def check_if_login_needed(page: Page, test_url: str) -> bool:
    try:
        await page.goto(test_url, timeout=PAGE_TIMEOUT, wait_until="load")
        if "signin" in page.url.lower() or "/ap/" in page.url:
            app_logger.info("Session invalid, login required.")
            return True
        await expect(page.locator("kat-table >> nth=0")).to_be_visible(timeout=WAIT_TIMEOUT)
        app_logger.info("Existing session still valid (metrics table found).")
        return False
    except Exception as e:
        app_logger.warning(f"Verification check failed: {e}. Assuming login is required.")
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

        async with page.expect_navigation(wait_until="domcontentloaded", timeout=WAIT_TIMEOUT):
            await page.get_by_label("Sign in").click()

        if "mfa" in page.url:
            app_logger.info("OTP challenge detected.")
            code = pyotp.TOTP(config['otp_secret_key']).now()
            await page.locator('input[id*="otp"]').fill(code)
            async with page.expect_navigation(wait_until="load", timeout=WAIT_TIMEOUT):
                await page.get_by_role("button", name="Sign in").click()

        app_logger.info("Login flow actions completed. Session will be verified.")
        return True

    except Exception as e:
        app_logger.critical(f"Login failed during actions: {e}", exc_info=DEBUG_MODE)
        await save_screenshot(page, "login_failure")
        return False

async def prime_master_session(browser: Browser) -> bool:
    app_logger.info("Priming master session")
    ctx = await browser.new_context()
    page = await ctx.new_page()
    try:
        if not await perform_login(page):
            return False

        first_store = TARGET_STORES[0]
        test_url = (
            f"https://sellercentral.amazon.co.uk/snowdash"
            f"?mons_sel_dir_mcid={first_store['merchant_id']}"
            f"&mons_sel_mkid={first_store['marketplace_id']}"
        )
        app_logger.info(f"Verifying session by navigating to dashboard: {test_url}")

        login_needed = await check_if_login_needed(page, test_url)
        if login_needed:
            app_logger.critical("Session verification failed: still requires login after login flow.")
            await save_screenshot(page, "session_verification_failure")
            return False

        await ctx.storage_state(path=STORAGE_STATE)
        app_logger.info("Saved new session state.")
        return True

    except Exception as e:
        app_logger.critical(f"An unexpected error occurred during session priming: {e}", exc_info=True)
        await save_screenshot(page, "session_priming_error")
        return False
    finally:
        await ctx.close()
