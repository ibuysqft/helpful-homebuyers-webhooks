"""DealMachine comps fetcher.

Uses Playwright to scrape sold comparable sales from DealMachine
for a given property address.
"""
from __future__ import annotations

import os
import re
import asyncio
import statistics
from typing import Optional


DEALMACHINE_EMAIL = os.getenv('DEALMACHINE_EMAIL', '')
DEALMACHINE_PASSWORD = os.getenv('DEALMACHINE_PASSWORD', '')
COMP_RADIUS_MILES = 0.5
COMP_COUNT = 4


def parse_comp_price(raw: str) -> Optional[int]:
    """Parse a price string like '$185,000' or '185K' into integer dollars."""
    if not raw:
        return None
    raw = raw.strip().upper().replace(',', '').replace('$', '').replace(' ', '')
    if raw.endswith('K'):
        try:
            return int(float(raw[:-1]) * 1000)
        except ValueError:
            return None
    match = re.search(r'(\d+)', raw)
    if not match:
        return None
    val = int(match.group(1))
    return val if val > 1000 else None  # filter out obviously wrong values


def filter_comps(prices: list[int]) -> list[int]:
    """Remove statistical outliers using IQR method (robust to single outliers)."""
    if len(prices) < 2:
        return prices
    sorted_prices = sorted(prices)
    n = len(sorted_prices)
    q1 = sorted_prices[n // 4]
    q3 = sorted_prices[(3 * n) // 4]
    iqr = q3 - q1
    if iqr == 0:
        return prices
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    return [p for p in prices if lower <= p <= upper]


def pull_comps_mock(address: str) -> list[int]:
    """Return deterministic mock comps for testing without Playwright."""
    return [165_000, 170_000, 172_000, 168_000]


async def _pull_comps_async(address: str) -> list[int]:
    """Playwright scraper — fetches sold comps from DealMachine."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        )
        page = await context.new_page()

        try:
            await page.goto('https://app.dealmachine.com/login', timeout=30_000)
            await page.fill('input[type="email"], input[name="email"]', DEALMACHINE_EMAIL)
            await page.fill('input[type="password"], input[name="password"]', DEALMACHINE_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state('networkidle', timeout=20_000)

            await page.goto(f'https://app.dealmachine.com/comps?address={address}', timeout=30_000)
            await page.wait_for_load_state('networkidle', timeout=20_000)

            await page.wait_for_selector(
                '[data-testid="comp-price"], .comp-price, .sold-price', timeout=10_000,
            )
            price_elements = await page.query_selector_all(
                '[data-testid="comp-price"], .comp-price, .sold-price',
            )

            prices = []
            for el in price_elements[:20]:
                raw = await el.inner_text()
                price = parse_comp_price(raw)
                if price:
                    prices.append(price)

            return filter_comps(prices)

        except Exception as exc:
            print(f"[comps] DealMachine scrape failed for {address}: {exc}")
            return []
        finally:
            await browser.close()


def pull_comps(address: str) -> list[int]:
    """Synchronous wrapper around the async Playwright scraper."""
    return asyncio.run(_pull_comps_async(address))
