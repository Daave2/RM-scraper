# RM-scraper

Asynchronous scraper that collects performance metrics and inventory insights from Amazon Seller Central.
It uses [Playwright](https://playwright.dev/python/) to log in, scrape dashboard data for multiple stores, and send summaries to Google Chat webhooks.

## Features
- Scrapes snowdash metrics and "INF" inventory data for each target store.
- Posts per-store reports and an aggregate summary to Chat webhooks.
- Retries failed requests and logs JSON results to `output/submissions.jsonl`.

## Requirements
- Python 3.12+
- Google Chat webhook URL(s)
- An Amazon Seller Central account with two-factor authentication

Install dependencies and Playwright browsers:
```bash
pip install -r requirements.txt
playwright install
```

## Configuration
Copy `config.example.json` to `config.json` and fill in your details:
```jsonc
{
  "login_email": "you@example.com",
  "login_password": "your_password",
  "otp_secret_key": "OTP_SECRET",
  "chat_webhook_url": "https://chat.googleapis.com/...",
  "summary_chat_webhook_url": "https://chat.googleapis.com/...",
  "target_stores": [
    { "merchant_id": "AMAZON_MERCHANT_ID", "marketplace_id": "MARKETPLACE_ID", "store_name": "My Store" }
  ]
}
```
- `debug` in the config toggles headless mode.
- `target_stores` is a list of stores to scrape.

## Usage
Run the scraper once the configuration is in place:
```bash
python scraper.py
```
Results and screenshots (if any) are saved to the `output/` directory, and logs are written to `app.log`.

## Project Structure
- `scraper.py` – orchestrates the scraping flow.
- `src/settings.py` – loads config and sets up logging.
- `src/auth.py` – handles login and session management.
- `src/metrics.py` – scrapes performance metrics and INF items.
- `src/notifications.py` – posts reports to Google Chat.
- `src/utils.py` – helper functions.

## Development
There are currently no automated tests, but you can verify syntax:
```bash
python -m py_compile $(git ls-files '*.py')
```
