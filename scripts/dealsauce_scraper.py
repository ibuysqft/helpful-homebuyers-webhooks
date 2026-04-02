#!/usr/bin/env python3
"""
DealSauce cash buyer scraper.

Logs into app.dealsauce.io, paginates through the buyer list,
and upserts every buyer into Supabase cash_buyers.

Usage:
    python scripts/dealsauce_scraper.py

Credentials (from ~/.hhb/credentials.env):
    DEALSAUCE_EMAIL
    DEALSAUCE_PASSWORD
    SUPABASE_URL
    SUPABASE_KEY
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from supabase import create_client

# ── Load HHB credentials ──────────────────────────────────────────────────────
_CREDS_FILE = Path.home() / ".hhb" / "credentials.env"
if _CREDS_FILE.exists():
    for _line in _CREDS_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DEALSAUCE_EMAIL    = os.environ.get("DEALSAUCE_EMAIL", "")
DEALSAUCE_PASSWORD = os.environ.get("DEALSAUCE_PASSWORD", "")
SUPABASE_URL       = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY       = os.environ.get("SUPABASE_KEY", "")

LOGIN_URL  = "https://app.dealsauce.io/login"
BUYERS_URL = "https://app.dealsauce.io/buyers"

# CSS selectors — update after inspecting the live page if these don't match
SEL_EMAIL_INPUT    = "input[type='email'], input[name='email']"
SEL_PASSWORD_INPUT = "input[type='password'], input[name='password']"
SEL_SUBMIT_BUTTON  = "button[type='submit']"
SEL_BUYER_ROW      = ".buyer-card, tr.buyer-row, [data-testid='buyer-row'], tbody tr"
SEL_NEXT_PAGE      = "button[aria-label='Next page'], a.pagination-next, .next-page, [data-testid='next-page']"

log = logging.getLogger("dealsauce_scraper")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def _parse_price(raw: str) -> float:
    """Parse '$1.2M', '$850K', '1200000' → float. Returns 0.0 on parse failure."""
    s = raw.replace("$", "").replace(",", "").strip().upper()
    if s.endswith("M"):
        try:
            return float(s[:-1]) * 1_000_000
        except ValueError:
            return 0.0
    if s.endswith("K"):
        try:
            return float(s[:-1]) * 1_000
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_states(raw: str) -> list:
    """'CA, TX, FL' → ['CA', 'TX', 'FL']"""
    return [s.strip().upper() for s in raw.replace(";", ",").split(",") if s.strip()]


def _parse_cities(raw: str) -> list:
    """'Los Angeles, Houston' → ['Los Angeles', 'Houston']"""
    return [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]


async def _get_text(page, row, *selectors: str) -> str:
    """Try each selector against the row element; return first non-empty result."""
    for sel in selectors:
        try:
            el = await row.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and text.strip():
                    return text.strip()
        except Exception:
            continue
    return ""


async def _login(page) -> None:
    log.info("Navigating to login page…")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.fill(SEL_EMAIL_INPUT, DEALSAUCE_EMAIL)
    await page.fill(SEL_PASSWORD_INPUT, DEALSAUCE_PASSWORD)
    await page.click(SEL_SUBMIT_BUTTON)
    # Wait for redirect away from /login
    await page.wait_for_function(
        "() => !window.location.pathname.includes('/login')",
        timeout=15_000,
    )
    log.info("Login successful — now at %s", page.url)


async def _scrape_page(page) -> list:
    """Extract buyer records from the current page."""
    await page.wait_for_selector(SEL_BUYER_ROW, timeout=10_000)
    rows = await page.query_selector_all(SEL_BUYER_ROW)

    buyers = []
    for row in rows:
        first_name = await _get_text(page, row, ".first-name", "[data-field='firstName']", "td:nth-child(1)")
        last_name  = await _get_text(page, row, ".last-name",  "[data-field='lastName']",  "td:nth-child(2)")
        email      = await _get_text(page, row, ".email",      "[data-field='email']",      "td:nth-child(3)")
        phone      = await _get_text(page, row, ".phone",      "[data-field='phone']",      "td:nth-child(4)")
        company    = await _get_text(page, row, ".company",    "[data-field='company']",    "td:nth-child(5)")
        price_min  = await _get_text(page, row, ".price-min",  "[data-field='priceMin']",   "td:nth-child(6)")
        price_max  = await _get_text(page, row, ".price-max",  "[data-field='priceMax']",   "td:nth-child(7)")
        markets    = await _get_text(page, row, ".markets",    "[data-field='markets']",    "td:nth-child(8)")
        states     = await _get_text(page, row, ".states",     "[data-field='states']",     "td:nth-child(9)")

        email = email.lower().strip()
        phone = phone.strip()

        if not email and not phone:
            log.warning("Skipping buyer with no email or phone: %s %s", first_name, last_name)
            continue

        buyers.append({
            "first_name":       first_name or None,
            "last_name":        last_name or None,
            "email":            email or None,
            "phone":            phone or None,
            "company":          company or None,
            "price_range_min":  _parse_price(price_min) if price_min else None,
            "price_range_max":  _parse_price(price_max) if price_max else None,
            "preferred_cities": _parse_cities(markets) if markets else [],
            "preferred_states": _parse_states(states) if states else [],
            "status":           "active",
        })

    return buyers


async def _has_next_page(page) -> bool:
    btn = await page.query_selector(SEL_NEXT_PAGE)
    if not btn:
        return False
    disabled = await btn.get_attribute("disabled")
    aria_disabled = await btn.get_attribute("aria-disabled")
    return disabled is None and aria_disabled != "true"


async def scrape_all_buyers() -> list:
    buyers = []
    cdp_endpoint = os.getenv("CDP_ENDPOINT", "http://localhost:9222")

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
            log.info("Connected to existing Chrome via CDP at %s", cdp_endpoint)
        except Exception:
            log.info("No CDP at %s — launching new Chromium", cdp_endpoint)
            browser = await pw.chromium.launch(headless=True)

        context = await browser.new_context()
        page    = await context.new_page()

        await _login(page)
        await page.goto(BUYERS_URL, wait_until="domcontentloaded")

        page_num = 1
        while True:
            log.info("Scraping page %d…", page_num)
            try:
                page_buyers = await _scrape_page(page)
            except PlaywrightTimeout:
                log.warning("Timed out on page %d — committing scraped so far", page_num)
                break

            buyers.extend(page_buyers)
            log.info("  Page %d: %d buyers (total: %d)", page_num, len(page_buyers), len(buyers))

            if not await _has_next_page(page):
                break

            await page.click(SEL_NEXT_PAGE)
            await page.wait_for_load_state("networkidle", timeout=10_000)
            page_num += 1

        await context.close()

    return buyers


def upsert_buyers(buyers: list) -> tuple:
    """Upsert all buyers into Supabase cash_buyers. Returns (added, skipped)."""
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    added = skipped = 0

    for buyer in buyers:
        conflict_col = "email" if buyer.get("email") else "phone"
        if not buyer.get(conflict_col):
            skipped += 1
            continue
        try:
            sb.table("cash_buyers").upsert(buyer, on_conflict=conflict_col).execute()
            added += 1
        except Exception as exc:
            log.error("Upsert failed for %s: %s", buyer.get("email") or buyer.get("phone"), exc)
            skipped += 1

    return added, skipped


async def main():
    if not DEALSAUCE_EMAIL or not DEALSAUCE_PASSWORD:
        log.error("DEALSAUCE_EMAIL and DEALSAUCE_PASSWORD must be set in ~/.hhb/credentials.env")
        sys.exit(1)
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("SUPABASE_URL and SUPABASE_KEY must be set in ~/.hhb/credentials.env")
        sys.exit(1)

    log.info("DealSauce scraper starting…")
    buyers = await scrape_all_buyers()

    if not buyers:
        log.warning("No buyers scraped — check selectors or login credentials.")
        sys.exit(1)

    added, skipped = upsert_buyers(buyers)
    print(f"DealSauce scraper complete: {added} upserted, {skipped} skipped (no email/phone)")


if __name__ == "__main__":
    asyncio.run(main())
