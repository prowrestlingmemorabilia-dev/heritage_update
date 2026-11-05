"""
Heritage / Pristine Auction Update
----------------------------------
Heritage side matches the working Heritage update script:
- On GitHub Actions: launches headless Chromium, loads cookies from HERITAGE_COOKIES
- Locally: connects to Chrome on port 9222
- Loads existing Heritage records, visits each item URL
- If price unchanged → skip
- If price changed → grab bid count (prefer buyer's premium price) and upsert
- If no price and end_time +1h passed → delete

Pristine side is search-page based:
- Scrapes wrestling category + crossover searches
- Matches by item name
- Only opens detail URL when search-page price differs from Supabase

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
from urllib.parse import quote

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
    if not title:
        return ""
    return re.sub(r"\s+", " ", title.lower().strip())

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

def fetch_existing(supabase: Client, house: str, with_end_time: bool = False) -> List[dict]:
    fields = "id, current_price, item_url, bid_count"
    if with_end_time:
        fields += ", end_time"
    try:
        resp = (
            supabase.table("auctions")
            .select(fields)
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
# HERITAGE: check one item (matches working heritage_update.py)
# ---------------------------------------------------------------------------
def check_heritage_item(page: Page, record: dict) -> Optional[dict]:
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
        # Prefer buyer's premium price when present
        bp_el = page.locator("span.current-bid-BP").first
        if bp_el.count() > 0:
            price_text = bp_el.inner_text().strip()

        if not price_text:
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

        # If no price, only delete if end_time has passed by at least 1 hour
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
            title = page.title()
            body_snip = page.locator("body").inner_text()[:180].replace("\n", " ")
            log.info("  ENDED or missing %s (title: %s | snippet: %s) — marking for delete", item_id, title, body_snip)
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
            "auction_house": "Heritage Auctions",
            "current_price": current_price,
            "bid_count": bid_count,
            "is_hot": bid_count >= HOT_BID_THRESHOLD,
        }

    except Exception as e:
        log.warning("  Failed to check %s: %s", item_id, str(e)[:80])
        return None

# ---------------------------------------------------------------------------
# PRISTINE: search-page scrape, detail only on price change
# ---------------------------------------------------------------------------
def scrape_pristine_search_page(page: Page, url: str, label: str) -> Dict[str, float]:
    found: Dict[str, float] = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        slow_wait(page, 1.8)
        links = page.locator("a[href*='/a']").all()
        if len(links) == 0:
            title = page.title()
            body_snip = page.locator("body").inner_text()[:180].replace("\n", " ")
            log.info("  Pristine '%s': 0 links (title: %s | snippet: %s)", label, title, body_snip)
        else:
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

def check_pristine_detail(page: Page, record: dict, new_price: float) -> Optional[dict]:
    url = record.get("item_url")
    item_id = record.get("id")
    if not url:
        return None
    try:
        human_delay(0.8, 1.6)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        slow_wait(page, 1.0)

        bid_count = int(record.get("bid_count") or 0)
        for sel in ["span.bid-count", ".bids", "[class*='bid']"]:
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
            "auction_house": "Pristine Auction",
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
        all_to_delete = []
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
                    log.error("No valid cookies. Aborting to avoid false deletions.")
                    send_email("❌ Heritage/Pristine Update FAILED", "HERITAGE_COOKIES missing or invalid.\nNo records were checked or deleted.")
                    browser.close()
                    sys.exit(1)

                if not verify_session(page):
                    log.error("Cookies did not produce a usable session. Aborting.")
                    send_email("❌ Heritage/Pristine Update FAILED", "Cookies loaded but session check failed (bot challenge or not logged in).\nNo records were checked or deleted.\nRefresh the HERITAGE_COOKIES secret.")
                    browser.close()
                    sys.exit(1)
            else:
                log.info("Connecting to Chrome on %s ...", CDP_URL)
                browser = p.chromium.connect_over_cdp(CDP_URL)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()

            # ----- Heritage (matches working update script) -----
            log.info("=== Heritage Auctions ===")
            heritage_existing = fetch_existing(supabase, "Heritage Auctions", with_end_time=True)
            summary["Heritage Auctions"] = {"checked": 0, "updated": 0, "skipped": 0, "deleted": 0}

            for idx, record in enumerate(heritage_existing, 1):
                log.info("[%d/%d] %s", idx, len(heritage_existing), record.get("id"))
                result = check_heritage_item(page, record)
                summary["Heritage Auctions"]["checked"] += 1
                if result is None:
                    summary["Heritage Auctions"]["skipped"] += 1
                elif result.get("_delete"):
                    all_to_delete.append(result["id"])
                    summary["Heritage Auctions"]["deleted"] += 1
                else:
                    all_to_update.append(result)
                    summary["Heritage Auctions"]["updated"] += 1

                if random.random() < 0.15:
                    human_delay(1.8, 3.5)

            # ----- Pristine (search-page comparison) -----
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
                    updated = check_pristine_detail(page, rec, price)
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

        if all_to_delete:
            supabase.table("auctions").delete().in_("id", all_to_delete).execute()
            log.info("Deleted %d ended Heritage auctions", len(all_to_delete))
        else:
            log.info("No ended auctions to delete.")

        log.info("Done.")

        lines = ["Heritage / Pristine Update Report", "=" * 40, ""]
        for house, stats in summary.items():
            if "checked" in stats:
                lines.append(f"{house}: checked {stats['checked']} | updated {stats['updated']} | deleted {stats['deleted']} | skipped {stats['skipped']}")
            else:
                lines.append(f"{house}: matched {stats.get('matched', 0)} | updated {stats.get('updated', 0)} | skipped {stats.get('skipped', 0)}")
        lines.append("")
        if all_to_update:
            lines.append("Price changes:")
            for r in all_to_update:
                lines.append(f"  {r['id']} → ${r['current_price']:.2f} ({r['bid_count']} bids)")
            lines.append("")
        if all_to_delete:
            lines.append("Deleted:")
            for eid in all_to_delete:
                lines.append(f"  {eid}")
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
