"""Offer engine orchestrator.

Chains: fetch_redfin_photos → grade_condition → pull_comps → calculate_offer
Optionally persists results to Supabase deals table.
"""
from __future__ import annotations

import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from offer_engine.vision import grade_condition, fetch_redfin_photos
from offer_engine.comps import pull_comps
from offer_engine.calculator import calculate_offer

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY') or os.getenv('SUPABASE_KEY')


def analyze_deal(
    address: str,
    redfin_url: Optional[str],
    sqft: int,
    persist: bool = False,
    ghl_opportunity_id: Optional[str] = None,
) -> dict:
    """Run the full offer analysis pipeline for a property.

    Args:
        address: Full property address
        redfin_url: Redfin listing URL for photo scraping (optional)
        sqft: Property square footage
        persist: If True, upsert results into Supabase deals table
        ghl_opportunity_id: GHL opportunity ID to link the deal record

    Returns:
        dict with offer_price, condition_grade, vision_confidence,
        avg_comp, repair_cost, comps_json, ai_reasoning, and optionally deal_id
    """
    photo_urls = fetch_redfin_photos(redfin_url) if redfin_url else []
    vision_result = grade_condition(photo_urls=photo_urls, address=address)

    comps = pull_comps(address)

    if comps:
        offer_result = calculate_offer(
            comps=comps, sqft=sqft, condition_grade=vision_result['condition_grade'],
        )
    else:
        offer_result = {'offer_price': None, 'avg_comp': None, 'repair_cost': None, 'arv': None}

    result = {
        'address': address,
        'sqft': sqft,
        'redfin_url': redfin_url,
        'condition_grade': vision_result['condition_grade'],
        'vision_confidence': vision_result['vision_confidence'],
        'ai_reasoning': vision_result['reasoning'],
        'offer_price': offer_result['offer_price'],
        'avg_comp': offer_result['avg_comp'],
        'repair_cost': offer_result['repair_cost'],
        'comps_json': comps,
        'ghl_opportunity_id': ghl_opportunity_id,
    }

    if persist and SUPABASE_URL and SUPABASE_KEY:
        from supabase import create_client
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)

        # Only send columns that exist in the flywheel schema
        DB_COLUMNS = {
            'address', 'sqft', 'redfin_url', 'condition_grade', 'vision_confidence',
            'ai_reasoning', 'offer_price', 'comps_json', 'ghl_opportunity_id',
        }
        payload = {k: v for k, v in result.items() if k in DB_COLUMNS and v is not None}
        payload['comps_json'] = comps

        if ghl_opportunity_id:
            existing = (
                sb.table('deals')
                .select('id')
                .eq('ghl_opportunity_id', ghl_opportunity_id)
                .execute()
            )
            if existing.data:
                deal_id = existing.data[0]['id']
                sb.table('deals').update(payload).eq('id', deal_id).execute()
                result['deal_id'] = deal_id
                return result

        db_result = sb.table('deals').insert({**payload, 'status': 'ready_to_call'}).execute()
        result['deal_id'] = db_result.data[0]['id']

    return result
