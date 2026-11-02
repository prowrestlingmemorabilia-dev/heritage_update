"""
Heritage Auctions – Update Only
--------------------------------
- On GitHub Actions: launches headless Chromium, loads cookies from HERITAGE_COOKIES secret
- Locally: connects to Chrome on port 9222 (assumes already logged in)
- Loads existing Heritage records from Supabase
- Visits each item URL
- If current price is unchanged → skip immediately
- If price changed → grab bid count and upsert

Environment variables:
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  HERITAGE_COOKIES (JSON array of cookies, preferred on GitHub Actions)
  HERITAGE_USERNAME, HERITAGE_PASSWORD (fallback only)
"""

import os
import sys
import re
import time
import random
import logging
import traceback
import smtplib
import json
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from playwright.sync_api import sync_playwright, Page, BrowserContext
from playwright_stealth import Stealth
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

def send_email(subject: str, body: str):
    email_from = os.environ.get("EMAIL_FROM")
    email_to = os.environ.get("EMAIL_TO")
    email_pass = os.environ.get("EMAIL_PASSWORD")
    if not (email_from and email_to and email_pass):
        log.info("Email not configured. Skipping.")
        return
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(email_from, email_pass)
            server.send_message(msg)
        log.info("Email sent to %s", email_to)
    except Exception as e:
        log.error("Email failed: %s", e)

def get_supabase() -> Client:
    return create_client(require_env("SUPABASE_URL"), require_env("SUPABASE_SERVICE_KEY"))

def fetch_existing_heritage(supabase: Client) -> List[dict]:
    """Return list of Heritage records with id, current_price, item_url, end_time."""
    try:
        resp = (
            supabase.table("auctions")
            .select("id, current_price, item_url, bid_count, end_time")
            .eq("auction_house", AUCTION_HOUSE_NAME)
            .execute()
        )
        rows = resp.data or []
        log.info("Loaded %d existing Heritage records from Supabase", len(rows))
        return rows
    except Exception as e:
        log.warning("Could not load existing Heritage records: %s", e)
        return []

def load_cookies(context: BrowserContext) -> bool:
    """Load cookies from HERITAGE_COOKIES env var (JSON array). Returns True if loaded."""
    raw = os.environ.get("HERITAGE_COOKIES")
    if not raw:
        log.warning("HERITAGE_COOKIES not set.")
        return False
    try:
        cookies = json.loads(raw)
        if not isinstance(cookies, list) or not cookies:
            log.warning("HERITAGE_COOKIES is empty or not a list.")
            return False
        context.add_cookies(cookies)
        log.info("Loaded %d cookies into browser context.", len(cookies))
        return True
    except Exception as e:
        log.error("Failed to parse/load HERITAGE_COOKIES: %s", e)
        return False

def verify_session(page: Page) -> bool:
    """Quick check that we appear logged in (or at least not blocked)."""
    try:
        page.goto("https://www.ha.com/", wait_until="domcontentloaded", timeout=45000)
        slow_wait(page, 3)
        body = page.locator("body").inner_text()[:600].lower()
        title = page.title()
        log.info("Session check — title: %s", title)
        if any(x in body for x in ["checking your browser", "just a moment", "verify you are human", "cloudflare"]):
            log.warning("Bot challenge still present after cookies.")
            return False
        if "sign in" in body and "register" in body and "my account" not in body:
            log.warning("Looks like we are not logged in.")
            return False
        log.info("Session appears usable.")
        return True
    except Exception as e:
        log.warning("Session verification error: %s", str(e)[:150])
        return False

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

        # If we got no price at all, only delete if end_time has passed by at least 1 hour
        if current_price == 0.0 and not price_text:
            end_iso = record.get("end_time")
            if end_iso:
                try:
                    from datetime import datetime, timezone, timedelta
                    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) < end_dt + timedelta(hours=1):
                        log.info("  No price but end_time not yet +1h for %s — skipping delete", item_id)
                        return None
                except Exception:
                    pass
            log.info("  ENDED or missing %s — marking for delete", item_id)
            return {"id": item_id, "_delete": True}

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
        to_delete = []
        checked = 0
        skipped = 0

        with sync_playwright() as p:
            if os.environ.get("GITHUB_ACTIONS"):
                log.info("Running on GitHub Actions — launching stealth headless Chromium")
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--window-size=1920,1080",
                    ]
                )
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    locale="en-US",
                    timezone_id="America/Chicago",
                )
                Stealth().apply_stealth_sync(context)
                page = context.new_page()

                if not load_cookies(context):
                    log.error("No valid cookies. Aborting to avoid false deletions.")
                    send_email("❌ Heritage Update FAILED", "HERITAGE_COOKIES missing or invalid.\nNo records were checked or deleted.")
                    browser.close()
                    sys.exit(1)

                if not verify_session(page):
                    log.error("Cookies did not produce a usable session. Aborting.")
                    send_email("❌ Heritage Update FAILED", "Cookies loaded but session check failed (bot challenge or not logged in).\nNo records were checked or deleted.\nRefresh the HERITAGE_COOKIES secret.")
                    browser.close()
                    sys.exit(1)
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
                elif result.get("_delete"):
                    to_delete.append(result["id"])
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

        if to_delete:
            supabase.table("auctions").delete().in_("id", to_delete).execute()
            log.info("Deleted %d ended Heritage auctions", len(to_delete))
        else:
            log.info("No ended auctions to delete.")

        log.info("Done. Checked %d | Updated %d | Deleted %d | Skipped %d", checked, len(to_update), len(to_delete), skipped)

        # Build and send summary email
        lines = [
            "Heritage Update Report",
            "=" * 40,
            "",
            f"Checked:  {checked}",
            f"Updated:  {len(to_update)}",
            f"Deleted:  {len(to_delete)}",
            f"Skipped:  {skipped}",
            "",
        ]
        if to_update:
            lines.append("Price changes:")
            for r in to_update:
                lines.append(f"  {r['id']} → ${r['current_price']:.2f} ({r['bid_count']} bids)")
            lines.append("")
        if to_delete:
            lines.append("Deleted:")
            for eid in to_delete:
                lines.append(f"  {eid}")
            lines.append("")
        lines.append("SUCCESS")
        send_email("✅ Heritage Update Success", "\n".join(lines))

    except Exception:
        error_detail = traceback.format_exc()
        log.error("Script failed:\n%s", error_detail)
        send_email("❌ Heritage Update FAILED", f"Heritage Update FAILED\n\n{error_detail}")
        sys.exit(1)

    if sys.stdin.isatty():
        input("\nPress Enter to close...")

if __name__ == "__main__":
    main()
