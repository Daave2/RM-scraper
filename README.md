# RM Scraper

## Overview
This project contains an automated scraper for pulling performance metrics from Amazon Seller Central for a list of stores. It logs in using Playwright, collects picker performance data and Item Not Found (INF) information, then posts formatted reports to Google Chat webhooks. Results are also saved locally as JSONL logs.

## Setup
1. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

2. **Create `config.json`**
   Copy `config.example.json` to `config.json` and fill in your credentials and webhook URLs. The most important fields are your Amazon login details, OTP secret for MFA and a list of stores under `target_stores`.

   Example configuration:
   ```json
{
    "debug": false,
    "login_url": "https://sellercentral.amazon.co.uk/ap/signin",
    "login_email": "you@example.com",
    "login_password": "your_password",
    "otp_secret_key": "OTP_SECRET",
    "chat_webhook_url": "https://chat.googleapis.com/...",
    "summary_chat_webhook_url": "https://chat.googleapis.com/...",
    "target_stores": [
      {
        "merchant_id": "AMAZON_MERCHANT_ID_1",
        "marketplace_id": "YOUR_MARKETPLACE_ID_1",
        "store_name": "Morrisons - Example Store 1"
      },
      {
        "merchant_id": "AMAZON_MERCHANT_ID_2",
        "marketplace_id": "YOUR_MARKETPLACE_ID_2",
        "store_name": "Morrisons - Example Store 2"
      }
    ]
}
   ```

## Running Locally
Run the scraper directly:
```bash
python scraper.py
```
Logs will be written to `app.log` and raw results to the `output/` directory.

## Running via GitHub Actions
The workflow `.github/workflows/run-scraper.yml` can execute the scraper on a schedule or when manually triggered. Configure the required secrets in your repository settings (login credentials, OTP secret, webhook URLs and `TARGET_STORES_JSON`). The workflow installs dependencies, creates `config.json` from these secrets, runs the scraper and uploads logs as artifacts.

## Configuration Options
Key values in `config.json`:
- `debug` – Run the browser in headed mode when `true`.
- `login_url` – Seller Central login page.
- `login_email` / `login_password` – Your Amazon credentials.
- `otp_secret_key` – Secret used to generate OTP codes.
- `chat_webhook_url` – Google Chat webhook for per-store reports.
- `summary_chat_webhook_url` – Webhook for aggregated fleet summaries.
- `target_stores` – Array of store objects, each with `merchant_id`, `marketplace_id` and `store_name`.

Other runtime constants like timeouts and thresholds reside in `src/settings.py`.
