"""
Heritage / Pristine Auction Update
----------------------------------
Lean price-update only. Based on the working Heritage update script.

- Loads existing Heritage and Pristine records from Supabase
- Visits each item URL
- If current price is unchanged → skip immediately
- If price changed → grab bid count and upsert

Environment variables:
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  HERITAGE_COOKIES (JSON array of cookies, preferred on GitHub Actions)
"""

import os
import sys
import re
import logging
import traceback
import smtplib
import json
from email.mime.text import MIMEText
from typing import Dict, List, Optional
from urllib.parse import quote, urljoin

from playwright.sync_api import sync_playwright, Page, BrowserContext
from playwright_stealth import Stealth
from supabase import create_client, Client

# =============================================================================
# CONFIG
# =============================================================================
CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")
HOT_BID_THRESHOLD = 5

PRISTINE_CROSSOVER = [
    "ronda rousey", "dave bautista", "brock lesnar", "hulk hogan", "dwayne johnson",
]

HOUSES = {
    "Heritage Auctions": {
        "price_selectors": [
            "div.item-value",
            ".current-bid",
            ".lot-current-bid",
            "span.current-bid",
            ".price",
        ],
        "bid_selectors": ["span#number-bids"],
    },
    "Pristine Auction": {
        "price_selectors": [
            ".current-bid",
            ".bid-price",
            ".price",
            "span.price",
            "[class*='price']",
        ],
        "bid_selectors": ["span.bid-count", ".bids", "[class*='bid']"],
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("HeritagePristineUpdate")

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

def fetch_existing(supabase: Client, house: str) -> List[dict]:
    """Return records with id, current_price, item_url, bid_count."""
    try:
        resp = (
            supabase.table("auctions")
            .select("id, current_price, item_url, bid_count")
            .eq("auction_house", house)
            .execute()
        )
        rows = resp.data or []
        log.info("Loaded %d existing %s records from Supabase", len(rows), house)
        return rows
    except Exception as e:
        log.warning("Could not load existing %s records: %s", house, e)
        return []

def load_cookies(context: BrowserContext) -> bool:
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
    try:
        page.goto("https://www.ha.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        body = page.locator("body").inner_text()[:600].lower()
        title = page.title()
        log.info("Session check — title: %s", title)
        if any(x in body for x in ["checking your browser", "just a moment", "verify you are human", "cloudflare"]):
            log.warning("Bot challenge still present after cookies.")
            return False
        log.info("Session appears usable.")
        return True
    except Exception as e:
        log.warning("Session verification error: %s", str(e)[:150])
        return False

# ---------------------------------------------------------------------------
# CHECK ONE ITEM
# ---------------------------------------------------------------------------
def norm_title(title: str) -> str:
    if not title:
        return ""
    return re.sub(r"\s+", " ", title.lower().strip())

def scrape_pristine_search_page(page: Page, url: str, label: str) -> Dict[str, float]:
    """Return {normalized_title: price} from one Pristine search/category page."""
    found: Dict[str, float] = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(1800)
        links = page.locator("a[href*='/a']").all()
        log.info("  Pristine '%s': %d candidate links", label, len(links))
        for link in links:
            title = (link.inner_text() or "").strip()
            if not title or len(title) < 8:
                continue
            container = link
            for _ in range(3):
                parent = container.locator("xpath=..").first
                if parent.count() == 0:
                    break
                container = parent
            text = container.inner_text()
            price = parse_price(text)
            if price > 0:
                found[norm_title(title)] = price
        log.info("  Pristine '%s': %d priced titles", label, len(found))
    except Exception as e:
        log.warning("  Pristine search failed for '%s': %s", label, str(e)[:80])
    return found

def check_item(page: Page, record: dict, house: str) -> Optional[dict]:
    """
    Visit the item page.
    Returns updated fields if price changed, None if unchanged or failed.
    """
    url = record.get("item_url")
    if not url:
        return None

    old_price = float(record.get("current_price") or 0)
    item_id = record.get("id")
    cfg = HOUSES.get(house, HOUSES["Heritage Auctions"])

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(800)

        price_text = ""
        for sel in cfg["price_selectors"]:
            el = page.locator(sel).first
            if el.count() > 0:
                price_text = el.inner_text().strip()
                break

        current_price = parse_price(price_text)

        if current_price == 0.0 and not price_text:
            log.info("  No price found for %s — skipping", item_id)
            return None

        if abs(current_price - old_price) < 0.01:
            log.info("  SKIP (price unchanged) %s — $%.2f", item_id, current_price)
            return None

        log.info("  PRICE CHANGED %s — was $%.2f now $%.2f", item_id, old_price, current_price)

        bid_count = int(record.get("bid_count") or 0)
        for sel in cfg["bid_selectors"]:
            el = page.locator(sel).first
            if el.count() > 0:
                raw = el.inner_text()
                m = re.search(r"(\d+)", raw)
                if m:
                    bid_count = int(m.group(1))
                    break

        return {
            "id": item_id,
            "auction_house": house,
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
        all_to_update = []
        summary = {}

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
                    log.warning("No Heritage cookies — Heritage checks may be limited.")
                else:
                    verify_session(page)
            else:
                log.info("Connecting to Chrome on %s ...", CDP_URL)
                browser = p.chromium.connect_over_cdp(CDP_URL)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()

            for house in ("Heritage Auctions",):
                log.info("=== %s ===", house)
                existing = fetch_existing(supabase, house)
                summary[house] = {"checked": 0, "updated": 0, "skipped": 0}

                for idx, record in enumerate(existing, 1):
                    log.info("[%d/%d] %s", idx, len(existing), record.get("id"))
                    result = check_item(page, record, house)
                    summary[house]["checked"] += 1
                    if result is None:
                        summary[house]["skipped"] += 1
                    else:
                        all_to_update.append(result)
                        summary[house]["updated"] += 1

            # ----- Pristine: search-page comparison, detail only on price change -----
            log.info("=== Pristine Auction ===")
            pristine_existing = fetch_existing(supabase, "Pristine Auction")
            pristine_by_title: Dict[str, List[dict]] = {}
            for rec in pristine_existing:
                key = norm_title(rec.get("item_name") or "")
                if key:
                    pristine_by_title.setdefault(key, []).append(rec)

            pristine_search_prices: Dict[str, float] = {}
            wrestling_url = "https://www.pristineauction.com/auctions/index/index/category/wrestling"
            pristine_search_prices.update(scrape_pristine_search_page(page, wrestling_url, "wrestling"))
            for kw in PRISTINE_CROSSOVER:
                encoded = quote(kw)
                url = f"https://www.pristineauction.com/auction/search/?term={encoded}&per_page=60"
                pristine_search_prices.update(scrape_pristine_search_page(page, url, kw))

            summary["Pristine Auction"] = {"matched": 0, "updated": 0, "skipped": 0}
            for title, price in pristine_search_prices.items():
                recs = pristine_by_title.get(title, [])
                if not recs:
                    continue
                summary["Pristine Auction"]["matched"] += len(recs)
                for rec in recs:
                    old = float(rec.get("current_price") or 0)
                    if abs(old - price) < 0.01:
                        summary["Pristine Auction"]["skipped"] += 1
                        continue
                    updated = check_item(page, rec, "Pristine Auction")
                    if updated:
                        all_to_update.append(updated)
                        summary["Pristine Auction"]["updated"] += 1

            if os.environ.get("GITHUB_ACTIONS"):
                browser.close()

        if all_to_update:
            supabase.table("auctions").upsert(all_to_update).execute()
            log.info("Updated %d records with new prices", len(all_to_update))
        else:
            log.info("No price changes found.")

        log.info("Done.")

        lines = ["Heritage / Pristine Update Report", "=" * 40, ""]
        for house, stats in summary.items():
            lines.append(f"{house}: checked {stats['checked']} | updated {stats['updated']} | skipped {stats['skipped']}")
        lines.append("")
        if all_to_update:
            lines.append("Price changes:")
            for r in all_to_update:
                lines.append(f"  {r['id']} → ${r['current_price']:.2f} ({r['bid_count']} bids)")
            lines.append("")
        lines.append("SUCCESS")
        send_email("✅ Heritage/Pristine Update Success", "\n".join(lines))

    except Exception:
        error_detail = traceback.format_exc()
        log.error("Script failed:\n%s", error_detail)
        send_email("❌ Heritage/Pristine Update FAILED", f"Update FAILED\n\n{error_detail}")
        sys.exit(1)

    if sys.stdin.isatty():
        input("\nPress Enter to close...")

if __name__ == "__main__":
    main()
