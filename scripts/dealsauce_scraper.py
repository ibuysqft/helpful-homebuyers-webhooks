#!/usr/bin/env python3
"""
DealSauce cash buyer scraper.

Daily flow:
  1. Log into app.dealsauce.io
  2. Trigger a new cash-buyer export via POST /publy/leads (export mode)
  3. Poll /publy/csv_exports/{id} until status == FINISHED (up to 10 min)
  4. Download the ZIP from /publy/export/{id}/download
  5. Extract CSV, map rows → cash_buyers, upsert into Supabase

Architecture notes:
- DealSauce is a white-label of Realeflow / Housefolios.
- Property search lives in an iframe: app.dealsauce.io/property-search
  → iframe src: search.dealsauce.io — auth uses HttpOnly session cookies.
- The live /publy/leads search API requires geolocation and returns 0
  results when called without a map center. Export mode works without it.

Usage:
    python scripts/dealsauce_scraper.py

Credentials (from ~/.hhb/credentials.env):
    DEALSAUCE_EMAIL
    DEALSAUCE_PASSWORD
    SUPABASE_URL
    SUPABASE_KEY  (or SUPABASE_SERVICE_ROLE_KEY)
"""
import asyncio
import csv
import io
import logging
import os
import sys
import time
import zipfile
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
# Accept either key name
SUPABASE_KEY       = (
    os.environ.get("SUPABASE_KEY")
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
)

LOGIN_URL          = "https://app.dealsauce.io/signin"
PROPERTY_SEARCH_URL = "https://app.dealsauce.io/property-search"
SEARCH_BASE        = "https://search.dealsauce.io"

SEL_EMAIL_INPUT    = 'input[placeholder="Email Address"]'
SEL_PASSWORD_INPUT = 'input[type="password"]'
SEL_SUBMIT_BUTTON  = 'button[type="submit"]'

EXPORT_POLL_INTERVAL = 10   # seconds between status polls
EXPORT_POLL_MAX      = 60   # max attempts (10 min)

log = logging.getLogger("dealsauce_scraper")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _first(*vals) -> str | None:
    return next((v.strip() for v in vals if (v or "").strip()), None)


def _extract_csv(raw: bytes) -> str:
    """Unzip raw bytes and decode the CSV inside."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        data = zf.read(name)
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return data.decode(enc).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    raise ValueError("Cannot decode CSV from ZIP")


def _row_to_buyer(row: dict) -> dict | None:
    """Map a DealSauce CSV row to a cash_buyers upsert payload."""
    email = _first(
        row.get("Contact1Email_1"),
        row.get("Contact1Email_2"),
        row.get("Contact1Email_3"),
    )
    if email:
        email = email.lower()

    phone = _first(
        row.get("Contact1Phone_1"),
        row.get("Contact1Phone_2"),
        row.get("Contact1Phone_3"),
    )

    mailing_addr  = _first(row.get("RecipientAddress"))
    mailing_city  = _first(row.get("RecipientCity"))
    mailing_state = (row.get("RecipientState") or "").strip().upper() or None
    mailing_zip   = _first(row.get("RecipientPostalCode"))

    if not email and not phone and not mailing_addr:
        return None

    return {
        "first_name":       _first(row.get("FirstName")),
        "last_name":        _first(row.get("LastName")),
        "email":            email,
        "phone":            phone,
        "mailing_address":  mailing_addr,
        "mailing_city":     mailing_city,
        "mailing_state":    mailing_state,
        "mailing_zip":      mailing_zip,
        "preferred_states": [mailing_state] if mailing_state else [],
        "status":           "active",
        "source":           "dealsauce",
        # Default scoring — upgraded from internal history once deal_count >= 1
        "score":            50,
        "score_source":     "dealsauce_import",
        "grade":            "D",
    }


# ── Playwright helpers ────────────────────────────────────────────────────────

async def _login(page) -> None:
    log.info("Logging in to DealSauce…")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await asyncio.sleep(1)
    await page.fill(SEL_EMAIL_INPUT, DEALSAUCE_EMAIL)
    await page.fill(SEL_PASSWORD_INPUT, DEALSAUCE_PASSWORD)
    await page.click(SEL_SUBMIT_BUTTON)
    await page.wait_for_function(
        "() => !window.location.pathname.includes('/signin')",
        timeout=20_000,
    )
    log.info("Login successful — at %s", page.url)


async def _init_sso(page) -> None:
    """Load the property-search iframe to set search.dealsauce.io session cookies."""
    log.info("Initializing SSO session via property-search iframe…")
    await page.goto(PROPERTY_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
    try:
        await page.wait_for_selector("iframe", timeout=20_000)
    except PlaywrightTimeout:
        log.warning("No iframe found — SSO may have failed")
        return
    await asyncio.sleep(5)
    log.info("SSO ready")


async def _trigger_export(ctx) -> str | None:
    """
    POST /publy/leads with export mode to kick off a new cash-buyer export.
    Returns the export ID string, or None on failure.
    """
    payload = {
        "filterProperties": {
            "places": [],
            "leadTypes": ["cash buyer"],
            "propertyMainCategory": "residential",
            "propertyTypes": {},
            "includeAllLeadTypes": False,
        },
        "pagination": {"pageSize": 25, "page": 1, "cursor": None},
        "sessionId": int(time.time() * 1000),
        "order": {"field": "lastSaleDate", "direction": "desc"},
        "selection": {"includeAll": True, "include": [], "exclude": [], "count": 0},
        "export": {"type": "leads", "format": "csv"},
    }
    resp = await ctx.request.post(
        f"{SEARCH_BASE}/publy/leads",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    if resp.status not in (200, 201, 202):
        log.error("Export trigger failed: HTTP %d", resp.status)
        return None

    body = await resp.json()
    export_id = (
        body.get("exportId")
        or body.get("id")
        or (body.get("export") or {}).get("id")
    )
    if export_id:
        log.info("Export triggered: %s", export_id)
    else:
        log.warning("Export trigger response had no exportId: %s", str(body)[:200])
    return export_id


async def _poll_export(ctx, export_id: str) -> bool:
    """Poll until the export status is FINISHED. Returns True on success."""
    url = f"{SEARCH_BASE}/publy/csv_exports/{export_id}"
    for attempt in range(EXPORT_POLL_MAX):
        await asyncio.sleep(EXPORT_POLL_INTERVAL)
        resp = await ctx.request.get(url)
        if resp.status != 200:
            log.warning("Poll attempt %d: HTTP %d", attempt + 1, resp.status)
            continue
        body = await resp.json()
        status = body.get("status", "")
        lead_count = body.get("leadCount", "?")
        log.info("Poll %d/%d: status=%s leads=%s", attempt + 1, EXPORT_POLL_MAX, status, lead_count)
        if status == "FINISHED":
            return True
        if status in ("FAILED", "ERROR"):
            log.error("Export failed with status: %s", status)
            return False
    log.error("Export timed out after %d polls", EXPORT_POLL_MAX)
    return False


async def _download_export(ctx, export_id: str) -> bytes | None:
    """Download the ZIP from /publy/export/{id}/download."""
    url = f"{SEARCH_BASE}/publy/export/{export_id}/download"
    resp = await ctx.request.get(url)
    if resp.status != 200:
        log.error("Download failed: HTTP %d", resp.status)
        return None
    raw = await resp.body()
    log.info("Downloaded %d bytes for export %s", len(raw), export_id)
    return raw


# ── Supabase upsert ───────────────────────────────────────────────────────────

def upsert_buyers(buyers: list) -> tuple:
    """Upsert buyers into Supabase cash_buyers. Returns (added, skipped)."""
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    added = skipped = 0
    for buyer in buyers:
        col = (
            "email" if buyer.get("email")
            else "phone" if buyer.get("phone")
            else "mailing_address"
        )
        try:
            sb.table("cash_buyers").upsert(buyer, on_conflict=col).execute()
            added += 1
        except Exception as exc:
            key = buyer.get("email") or buyer.get("phone") or buyer.get("mailing_address")
            log.debug("Upsert skipped %s: %s", key, str(exc)[:100])
            skipped += 1
    return added, skipped


# ── Main ──────────────────────────────────────────────────────────────────────

async def scrape_and_load() -> tuple:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        await _login(page)
        await _init_sso(page)

        # Trigger new export
        export_id = await _trigger_export(ctx)
        if not export_id:
            log.error("Could not trigger export — aborting")
            await ctx.close()
            return 0, 0

        # Poll until done
        ok = await _poll_export(ctx, export_id)
        if not ok:
            await ctx.close()
            return 0, 0

        # Download and parse
        raw = await _download_export(ctx, export_id)
        await ctx.close()

    if not raw:
        return 0, 0

    csv_text = _extract_csv(raw)
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    log.info("CSV: %d rows", len(rows))

    buyers = [b for r in rows if (b := _row_to_buyer(r)) is not None]
    log.info("Mapped %d buyers", len(buyers))

    if not buyers:
        log.warning("No mappable buyers in export")
        return 0, 0

    return upsert_buyers(buyers)


async def main():
    if not DEALSAUCE_EMAIL or not DEALSAUCE_PASSWORD:
        log.error("DEALSAUCE_EMAIL and DEALSAUCE_PASSWORD must be set")
        sys.exit(1)
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) must be set")
        sys.exit(1)

    log.info("DealSauce scraper starting…")
    added, skipped = await scrape_and_load()
    print(f"DealSauce scraper complete: {added} upserted, {skipped} skipped")


if __name__ == "__main__":
    asyncio.run(main())
