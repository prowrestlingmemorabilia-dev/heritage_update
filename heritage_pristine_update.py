"""
Heritage / Pristine Auction Update
----------------------------------
Fast price-update only.

1. Load existing records from Supabase (id, item_name, current_price, item_url)
2. Run broad keyword / category searches
3. Match search-page results by item name
4. If search-page price == Supabase price → skip
5. If price differs → open the detail URL, grab bid count, upsert

Environment variables:
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  HERITAGE_COOKIES (JSON array of cookies, preferred on GitHub Actions)
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
from urllib.parse import quote, urljoin

from playwright.sync_api import sync_playwright, Page, BrowserContext
from playwright_stealth import Stealth
from supabase import create_client, Client

# =============================================================================
# CONFIG
# =============================================================================
CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")
HOT_BID_THRESHOLD = 5

HERITAGE_KEYWORDS = [
    "wrestling", "wwe", "wwf", "wcw", "aew", "ecw",
    "hulk hogan", "stone cold steve austin", "dwayne johnson", "john cena", "brock lesnar",
    "ronda rousey", "ernie ladd", "wahoo mcdaniel", "roddy piper", "steve mcmichael",
    "ultimate warrior americana", "rhea ripley", "andre the giant",
    "cody rhodes", "Macho Man Randy Savage", "Shawn Michaels",
    "Bret Hart", "Triple H", "CM Punk", "Randy Orton", "Bill Goldberg", "Sgt Slaughter", "Mick Foley",
    "Vince McMahon", "Charlotte Flair", "Dusty Rhodes", "Iron Sheik", "Rey Mysterio", "Jake Roberts",
    "Torrie Wilson", "Dave Bautista", "Adam Copeland", "Stephanie McMahon", "penta zero miedo",
    "Maxwell Jacob Friedman", "Kurt Angle", "Undertaker", "Ric Flair", "Joe Anoa'i",
]

PRISTINE_CROSSOVER = [
    "ronda rousey", "dave bautista", "brock lesnar", "hulk hogan", "dwayne johnson",
]

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

def norm_title(title: str) -> str:
    """Normalize title for matching (lowercase, collapse whitespace)."""
    if not title:
        return ""
    return re.sub(r"\s+", " ", title.lower().strip())

def human_delay(min_sec: float = 1.0, max_sec: float = 2.2):
    delay = random.uniform(min_sec, max_sec)
    if random.random() < 0.08:
        delay += random.uniform(0.8, 2.0)
    time.sleep(delay)

def slow_wait(page: Page, seconds: float = 1.3):
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

def fetch_existing(supabase: Client, house: str) -> List[dict]:
    """Return records with id, item_name, current_price, item_url, bid_count."""
    try:
        resp = (
            supabase.table("auctions")
            .select("id, item_name, current_price, item_url, bid_count")
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
        slow_wait(page, 2.0)
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
# SEARCH-PAGE SCRAPERS (title + price only)
# ---------------------------------------------------------------------------
def scrape_heritage_search(page: Page, keyword: str) -> Dict[str, float]:
    """Return {normalized_title: price} from one Heritage search results page."""
    found: Dict[str, float] = {}
    encoded = quote(keyword)
    url = f"https://www.ha.com/c/search/results.zx?term={encoded}&mode=live&page=200%7E1"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        slow_wait(page, 2.0)
        items = page.locator("li.item-block.list, li.item-block")
        count = items.count()
        for i in range(count):
            el = items.nth(i)
            title_el = el.locator("a.item-title, a.item-title.short").first
            title = title_el.inner_text().strip() if title_el.count() > 0 else ""
            if not title:
                continue
            price_el = el.locator("div.item-value").first
            raw = price_el.inner_text().strip() if price_el.count() > 0 else ""
            price = parse_price(raw)
            if price > 0:
                found[norm_title(title)] = price
        log.info("  Heritage '%s': %d priced titles", keyword, len(found))
    except Exception as e:
        log.warning("  Heritage search failed for '%s': %s", keyword, str(e)[:80])
    return found

def scrape_pristine_search(page: Page, url: str, label: str) -> Dict[str, float]:
    """Return {normalized_title: price} from one Pristine search/category page."""
    found: Dict[str, float] = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        slow_wait(page, 1.8)
        links = page.locator("a[href*='/a']").all()
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

# ---------------------------------------------------------------------------
# DETAIL PAGE (only called when price differs)
# ---------------------------------------------------------------------------
def update_from_detail(page: Page, record: dict, house: str, new_price: float) -> Optional[dict]:
    """Open the item URL and return fields to upsert."""
    url = record.get("item_url")
    item_id = record.get("id")
    if not url:
        return None
    try:
        human_delay(0.8, 1.6)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        slow_wait(page, 1.0)

        bid_count = int(record.get("bid_count") or 0)
        for sel in ["span#number-bids", "span.bid-count", ".bids", "[class*='bid']"]:
            el = page.locator(sel).first
            if el.count() > 0:
                raw = el.inner_text()
                m = re.search(r"(\d+)", raw)
                if m:
                    bid_count = int(m.group(1))
                    break

        log.info("  UPDATED %s — $%.2f → $%.2f (%d bids)", item_id, record.get("current_price", 0), new_price, bid_count)
        return {
            "id": item_id,
            "auction_house": house,
            "current_price": new_price,
            "bid_count": bid_count,
            "is_hot": bid_count >= HOT_BID_THRESHOLD,
        }
    except Exception as e:
        log.warning("  Detail update failed for %s: %s", item_id, str(e)[:80])
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

            # ----- Heritage -----
            log.info("=== Heritage Auctions ===")
            heritage_existing = fetch_existing(supabase, "Heritage Auctions")
            heritage_by_title: Dict[str, List[dict]] = {}
            for rec in heritage_existing:
                key = norm_title(rec.get("item_name") or "")
                if key:
                    heritage_by_title.setdefault(key, []).append(rec)

            heritage_search_prices: Dict[str, float] = {}
            for kw in HERITAGE_KEYWORDS:
                heritage_search_prices.update(scrape_heritage_search(page, kw))
                human_delay(0.6, 1.2)

            summary["Heritage Auctions"] = {"matched": 0, "updated": 0, "skipped": 0}
            for title, price in heritage_search_prices.items():
                recs = heritage_by_title.get(title, [])
                if not recs:
                    continue
                summary["Heritage Auctions"]["matched"] += len(recs)
                for rec in recs:
                    old = float(rec.get("current_price") or 0)
                    if abs(old - price) < 0.01:
                        summary["Heritage Auctions"]["skipped"] += 1
                        continue
                    updated = update_from_detail(page, rec, "Heritage Auctions", price)
                    if updated:
                        all_to_update.append(updated)
                        summary["Heritage Auctions"]["updated"] += 1

            # ----- Pristine -----
            log.info("=== Pristine Auction ===")
            pristine_existing = fetch_existing(supabase, "Pristine Auction")
            pristine_by_title: Dict[str, List[dict]] = {}
            for rec in pristine_existing:
                key = norm_title(rec.get("item_name") or "")
                if key:
                    pristine_by_title.setdefault(key, []).append(rec)

            pristine_search_prices: Dict[str, float] = {}
            # Wrestling category
            wrestling_url = "https://www.pristineauction.com/auctions/index/index/category/wrestling"
            pristine_search_prices.update(scrape_pristine_search(page, wrestling_url, "wrestling"))
            human_delay(0.6, 1.2)
            # Crossover names
            for kw in PRISTINE_CROSSOVER:
                encoded = quote(kw)
                url = f"https://www.pristineauction.com/auction/search/?term={encoded}&per_page=60"
                pristine_search_prices.update(scrape_pristine_search(page, url, kw))
                human_delay(0.6, 1.2)

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
                    updated = update_from_detail(page, rec, "Pristine Auction", price)
                    if updated:
                        all_to_update.append(updated)
                        summary["Pristine Auction"]["updated"] += 1

            if os.environ.get("GITHUB_ACTIONS"):
                browser.close()

        if all_to_update:
            supabase.table("auctions").upsert(all_to_update).execute()
            log.info("Upserted %d price updates", len(all_to_update))
        else:
            log.info("No price changes found.")

        log.info("Done.")

        lines = ["Heritage / Pristine Update Report", "=" * 40, ""]
        for house, stats in summary.items():
            lines.append(f"{house}: matched {stats['matched']} | updated {stats['updated']} | skipped {stats['skipped']}")
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
