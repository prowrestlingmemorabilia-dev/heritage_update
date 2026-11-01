"""
Heritage Auctions – Update Only
--------------------------------
- On GitHub Actions: launches headless Chromium and logs in with secrets
- Locally: connects to Chrome on port 9222 (assumes already logged in)
- Loads existing Heritage records from Supabase
- Visits each item URL
- If current price is unchanged → skip immediately
- If price changed → grab bid count and upsert

Environment variables:
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  HERITAGE_USERNAME, HERITAGE_PASSWORD (used on GitHub Actions)
"""

import os
import sys
import re
import time
import random
import logging
import traceback
from typing import Dict, List, Optional

from playwright.sync_api import sync_playwright, Page
from supabase import create_client, Client

# =============================================================================
# CONFIG
# =============================================================================
CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")
AUCTION_HOUSE_NAME = "Heritage Auctions"
HOT_BID_THRESHOLD = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("HeritageUpdate")

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

def parse_price(price_str: str) -> float:
    if not price_str:
        return 0.0
    m = re.search(r"\$([\d,]+(?:\.\d+)?)", price_str)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return 0.0

def human_delay(min_sec: float = 1.4, max_sec: float = 3.2):
    delay = random.uniform(min_sec, max_sec)
    if random.random() < 0.12:
        delay += random.uniform(1.5, 3.5)
    time.sleep(delay)

def slow_wait(page: Page, seconds: float = 1.6):
    page.wait_for_timeout(int(seconds * 1000))

def get_supabase() -> Client:
    return create_client(require_env("SUPABASE_URL"), require_env("SUPABASE_SERVICE_KEY"))

def fetch_existing_heritage(supabase: Client) -> List[dict]:
    """Return list of Heritage records with id, current_price, item_url."""
    try:
        resp = (
            supabase.table("auctions")
            .select("id, current_price, item_url, bid_count")
            .eq("auction_house", AUCTION_HOUSE_NAME)
            .execute()
        )
        rows = resp.data or []
        log.info("Loaded %d existing Heritage records from Supabase", len(rows))
        return rows
    except Exception as e:
        log.warning("Could not load existing Heritage records: %s", e)
        return []

def login_to_heritage(page: Page):
    """Log in to Heritage using credentials from environment variables."""
    username = os.environ.get("HERITAGE_USERNAME")
    password = os.environ.get("HERITAGE_PASSWORD")
    if not username or not password:
        log.warning("HERITAGE_USERNAME or HERITAGE_PASSWORD not set. Skipping login.")
        return

    log.info("Logging in to Heritage...")
    try:
        page.goto("https://www.ha.com/c/login.zx", wait_until="domcontentloaded", timeout=30000)
        slow_wait(page, 2.0)

        user_field = page.locator("input[name='username'], input[type='email'], #username").first
        user_field.wait_for(state="visible", timeout=10000)
        user_field.fill(username)
        human_delay(0.6, 1.2)

        pass_field = page.locator("input[name='password'], input[type='password'], #password").first
        pass_field.fill(password)
        human_delay(0.5, 1.0)

        submit = page.locator("button[type='submit'], input[type='submit'], .login-button").first
        submit.click()
        slow_wait(page, 3.0)
        log.info("Login submitted.")
    except Exception as e:
        log.warning("Login issue (may need manual help or captcha): %s", str(e)[:120])

# ---------------------------------------------------------------------------
# CHECK ONE ITEM
# ---------------------------------------------------------------------------
def check_item(page: Page, record: dict) -> Optional[dict]:
    """
    Visit the item page.
    Returns updated fields if price changed, None if unchanged or failed.
    """
    url = record.get("item_url")
    if not url:
        return None

    old_price = float(record.get("current_price") or 0)
    item_id = record.get("id")

    try:
        human_delay(1.2, 2.4)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        slow_wait(page, 1.4)

        price_text = ""
        for sel in [
            "div.item-value",
            ".current-bid",
            ".lot-current-bid",
            "span.current-bid",
            ".price",
        ]:
            el = page.locator(sel).first
            if el.count() > 0:
                price_text = el.inner_text().strip()
                break

        current_price = parse_price(price_text)

        if abs(current_price - old_price) < 0.01:
            log.info("  SKIP (price unchanged) %s — $%.2f", item_id, current_price)
            return None

        log.info("  PRICE CHANGED %s — was $%.2f now $%.2f", item_id, old_price, current_price)

        bid_count = 0
        bid_el = page.locator("span#number-bids").first
        if bid_el.count() > 0:
            raw = bid_el.inner_text().strip()
            m = re.search(r"\d+", raw)
            if m:
                bid_count = int(m.group())

        return {
            "id": item_id,
            "auction_house": AUCTION_HOUSE_NAME,
            "current_price": current_price,
            "bid_count": bid_count,
            "is_hot": bid_count >= HOT_BID_THRESHOLD,
        }

    except Exception as e:
        log.warning("  Failed to check %s: %s", item_id, str(e)[:80])
        return None

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    try:
        supabase = get_supabase()
        existing = fetch_existing_heritage(supabase)

        if not existing:
            log.info("No Heritage records to update.")
            return

        to_update = []
        checked = 0
        skipped = 0

        with sync_playwright() as p:
            if os.environ.get("GITHUB_ACTIONS"):
                log.info("Running on GitHub Actions — launching headless Chromium")
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                login_to_heritage(page)
            else:
                log.info("Connecting to Chrome on %s ...", CDP_URL)
                browser = p.chromium.connect_over_cdp(CDP_URL)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()

            for idx, record in enumerate(existing, 1):
                log.info("[%d/%d] Checking %s", idx, len(existing), record.get("id"))
                result = check_item(page, record)
                checked += 1
                if result is None:
                    skipped += 1
                else:
                    to_update.append(result)

                if random.random() < 0.15:
                    human_delay(1.8, 3.5)

            if os.environ.get("GITHUB_ACTIONS"):
                browser.close()

        if to_update:
            supabase.table("auctions").upsert(to_update).execute()
            log.info("Updated %d records with new prices", len(to_update))
        else:
            log.info("No price changes found.")

        log.info("Done. Checked %d | Updated %d | Skipped %d", checked, len(to_update), skipped)

    except Exception:
        log.error("Script failed:\n%s", traceback.format_exc())
        sys.exit(1)

    if sys.stdin.isatty():
        input("\nPress Enter to close...")

if __name__ == "__main__":
    main()
