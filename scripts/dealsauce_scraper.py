#!/usr/bin/env python3
"""
DealSauce cash buyer scraper.

Logs into app.dealsauce.io, navigates to the property search to initialize
the search.dealsauce.io SSO session, then calls POST /publy/leads with
leadTypes=["cash buyer"] via an in-page fetch. Upserts every owner record
into Supabase cash_buyers.

Architecture notes:
- DealSauce is a white-label of Realeflow / Housefolios.
- Property search lives in an iframe: app.dealsauce.io/property-search
  → iframe src: search.dealsauce.io/Account/Account/LogOnWithToken?...
- Auth uses HttpOnly session cookies (not visible to JS). The token in the
  iframe URL performs the SSO exchange that sets search.dealsauce.io cookies.
- After iframe load, any page in the same browser context can call
  search.dealsauce.io APIs via fetch() with credentials: 'include'.
- Owner phone/email requires skip-trace credits (0 on current account).
  Records without email or phone are stored by mailing address.

Usage:
    python scripts/dealsauce_scraper.py

Credentials (from ~/.hhb/credentials.env):
    DEALSAUCE_EMAIL
    DEALSAUCE_PASSWORD
    SUPABASE_URL
    SUPABASE_KEY
"""
import asyncio
import json
import logging
import os
import sys
import time
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

LOGIN_URL          = "https://app.dealsauce.io/signin"
PROPERTY_SEARCH_URL = "https://app.dealsauce.io/property-search"
SEARCH_API_URL     = "https://search.dealsauce.io/publy/leads"
PAGE_SIZE          = 25

# Login form selectors (confirmed against live page — MUI text input, not email type)
SEL_EMAIL_INPUT    = 'input[placeholder="Email Address"]'
SEL_PASSWORD_INPUT = 'input[type="password"]'
SEL_SUBMIT_BUTTON  = 'button[type="submit"]'

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


async def _login(page) -> None:
    log.info("Navigating to login page: %s", LOGIN_URL)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await asyncio.sleep(1)  # allow MUI inputs to mount

    await page.fill(SEL_EMAIL_INPUT, DEALSAUCE_EMAIL)
    await page.fill(SEL_PASSWORD_INPUT, DEALSAUCE_PASSWORD)
    await page.click(SEL_SUBMIT_BUTTON)

    # Wait for redirect away from /signin
    await page.wait_for_function(
        "() => !window.location.pathname.includes('/signin')",
        timeout=20_000,
    )
    log.info("Login successful — now at %s", page.url)


async def _init_sso_session(context, main_page) -> None:
    """
    Navigate to /property-search so the iframe performs the SSO token exchange,
    setting session cookies on search.dealsauce.io for the entire browser context.
    """
    log.info("Initializing search.dealsauce.io SSO session via property-search iframe…")
    await main_page.goto(PROPERTY_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)

    # Wait for the iframe element — it carries the SSO token URL
    try:
        await main_page.wait_for_selector("iframe", timeout=20_000)
    except PlaywrightTimeout:
        log.warning("No iframe found on property-search — SSO may have failed")
        return

    # Give the iframe time to navigate and set cookies on search.dealsauce.io
    await asyncio.sleep(4)
    log.info("SSO iframe loaded — search.dealsauce.io session cookies should now be set")


def _build_payload(page_num: int, cursor=None) -> dict:
    return {
        "filterProperties": {
            "places": [],
            "leadTypes": ["cash buyer"],
            "propertyMainCategory": "residential",
            "propertyTypes": {},
            "includeAllLeadTypes": False,
        },
        "pagination": {
            "pageSize": PAGE_SIZE,
            "page": page_num,
            "cursor": cursor,
        },
        "sessionId": int(time.time() * 1000),
        "order": {"field": "distance", "direction": "asc"},
        "selection": {"includeAll": False, "include": [], "exclude": [], "count": 0},
        "export": {"type": None, "format": None},
    }


async def _fetch_page(search_page, page_num: int, cursor=None) -> dict:
    """
    Call POST /publy/leads from within the search.dealsauce.io page context.
    HttpOnly session cookies are included automatically via credentials: 'include'.
    """
    payload = _build_payload(page_num, cursor)
    result = await search_page.evaluate(
        """
        async (payload) => {
            const resp = await fetch('/publy/leads', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                body: JSON.stringify(payload),
            });
            if (!resp.ok) {
                const text = await resp.text();
                throw new Error('HTTP ' + resp.status + ': ' + text.slice(0, 200));
            }
            return await resp.json();
        }
        """,
        payload,
    )
    return result


def _record_to_buyer(rec: dict) -> dict | None:
    """
    Map a /publy/leads record to a cash_buyers row.

    Available without skip-trace credits:
      firstName, lastName, mailingAddress, mailingCity, mailingState, mailingZipCode
    Requires credits (not available on current account):
      phone, email (under rec['contacts'])
    """
    first_name = (rec.get("firstName") or "").strip()
    last_name  = (rec.get("lastName")  or "").strip()

    contacts = rec.get("contacts") or []
    email = next(
        (c.get("email", "").lower().strip() for c in contacts if c.get("email")),
        None,
    )
    phone = next(
        (c.get("phone", "").strip() for c in contacts if c.get("phone")),
        None,
    )

    mailing_address = (rec.get("mailingAddress") or "").strip()
    mailing_city    = (rec.get("mailingCity")    or "").strip()
    mailing_state   = (rec.get("mailingState")   or "").strip().upper()
    mailing_zip     = (rec.get("mailingZipCode") or "").strip()

    # Skip records with no identity whatsoever
    if not email and not phone and not mailing_address:
        return None

    state_from_mailing = [mailing_state] if mailing_state else []

    return {
        "first_name":        first_name or None,
        "last_name":         last_name  or None,
        "email":             email or None,
        "phone":             phone or None,
        "mailing_address":   mailing_address or None,
        "mailing_city":      mailing_city or None,
        "mailing_state":     mailing_state or None,
        "mailing_zip":       mailing_zip or None,
        "preferred_states":  state_from_mailing,
        "status":            "active",
        "source":            "dealsauce",
        # Default scoring — upgraded to internal history once deal_count >= 1
        "score":             50,
        "score_source":      "dealsauce_import",
        "grade":             "D",
    }


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
        main_page = await context.new_page()

        await _login(main_page)
        await _init_sso_session(context, main_page)

        # Open a new page on search.dealsauce.io to make API calls in its context
        search_page = await context.new_page()
        await search_page.goto(
            "https://search.dealsauce.io/Marketing/Leads",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        await asyncio.sleep(2)

        page_num = 1
        cursor = None
        has_next = True

        while has_next:
            log.info("Fetching cash buyer page %d (cursor=%s)…", page_num, cursor)
            try:
                data = await _fetch_page(search_page, page_num, cursor)
            except Exception as exc:
                log.error("API call failed on page %d: %s", page_num, exc)
                break

            records = data.get("list") or data.get("results") or data.get("data") or []
            if not records:
                log.warning("Empty result on page %d — stopping", page_num)
                break

            for rec in records:
                buyer = _record_to_buyer(rec)
                if buyer:
                    buyers.append(buyer)

            pagination = data.get("paginationData") or {}
            has_next = pagination.get("hasNextPage", False)
            cursor_obj = data.get("cursor")
            cursor = cursor_obj if cursor_obj else None

            log.info(
                "  Page %d: %d records (total so far: %d, hasNext=%s)",
                page_num, len(records), len(buyers), has_next,
            )

            if has_next:
                page_num += 1
                await asyncio.sleep(0.5)  # light rate limiting

        await context.close()

    return buyers


def upsert_buyers(buyers: list) -> tuple:
    """Upsert buyers into Supabase cash_buyers. Returns (added, skipped)."""
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    added = skipped = 0

    for buyer in buyers:
        # Prefer email, then phone, then mailing address as conflict key
        if buyer.get("email"):
            conflict_col = "email"
        elif buyer.get("phone"):
            conflict_col = "phone"
        elif buyer.get("mailing_address"):
            conflict_col = "mailing_address"
        else:
            skipped += 1
            continue

        try:
            sb.table("cash_buyers").upsert(buyer, on_conflict=conflict_col).execute()
            added += 1
        except Exception as exc:
            key = buyer.get("email") or buyer.get("phone") or buyer.get("mailing_address")
            log.error("Upsert failed for %s: %s", key, exc)
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
        log.warning(
            "No buyers scraped. Possible causes:\n"
            "  1. Login failed — check DEALSAUCE_EMAIL / DEALSAUCE_PASSWORD\n"
            "  2. SSO iframe did not load — network issue\n"
            "  3. API response format changed — check /publy/leads response keys\n"
            "  4. Account has 0 skip-trace credits — only mailing_address will be populated"
        )
        sys.exit(1)

    added, skipped = upsert_buyers(buyers)
    print(f"DealSauce scraper complete: {added} upserted, {skipped} skipped")


if __name__ == "__main__":
    asyncio.run(main())
