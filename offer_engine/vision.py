"""Claude Vision condition grader.

Fetches Redfin listing photos and uses Claude Vision to assign
a Fannie Mae C1-C6 condition grade with confidence score.
"""
from __future__ import annotations

import os
import re
import urllib.request
from typing import Optional

import anthropic

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

MAX_PHOTOS = 12

GRADING_PROMPT = """You are a licensed home inspector grading this property using Fannie Mae's C1-C6 condition scale.

Analyze all provided listing photos and assign ONE grade:
- C1: New or recently renovated, like new condition
- C2: Minor cosmetic updates needed (paint, carpet), well maintained
- C3: Light deferred maintenance, functional but dated
- C4: Moderate repairs needed, some systems aging
- C5: Heavy rehab, significant systems need replacement
- C6: Gut/rebuild, major structural or hazard issues

Property address: {address}

Respond in this exact format:
GRADE: C[1-6]
CONFIDENCE: [50-99]
REASONING: [2-3 sentences explaining key observations]

Focus on: kitchen, bathrooms, flooring, roof signals, foundation, HVAC, water/fire damage indicators."""


def parse_grade_response(text: str) -> dict:
    """Parse Claude's condition grade response into structured data."""
    match = re.search(r'\bC([1-6])\b', text, re.IGNORECASE)
    if not match:
        raise ValueError(f"No valid C1-C6 grade found in response: {text[:100]}")
    grade = f'C{match.group(1)}'

    conf_match = re.search(r'CONFIDENCE[:\s]+(\d+)', text, re.IGNORECASE)
    confidence = int(conf_match.group(1)) if conf_match else 70

    reason_match = re.search(r'REASONING[:\s]+(.+?)(?:\n|$)', text, re.IGNORECASE | re.DOTALL)
    reasoning = reason_match.group(1).strip() if reason_match else text.strip()

    return {
        'condition_grade': grade,
        'vision_confidence': min(99, max(50, confidence)),
        'reasoning': reasoning[:500],
    }


def grade_condition(photo_urls: list[str], address: str) -> dict:
    """Send listing photos to Claude Vision and return C1-C6 grade.

    Args:
        photo_urls: List of HTTPS image URLs from Redfin listing
        address: Property address (used in prompt for context)

    Returns:
        dict with condition_grade, vision_confidence, reasoning
    """
    if not photo_urls:
        return {
            'condition_grade': 'C3',
            'vision_confidence': 50,
            'reasoning': 'No photos available — defaulting to C3',
        }

    urls_to_use = photo_urls[:MAX_PHOTOS]

    content = []
    for url in urls_to_use:
        content.append({
            'type': 'image',
            'source': {'type': 'url', 'url': url},
        })

    content.append({
        'type': 'text',
        'text': GRADING_PROMPT.format(address=address),
    })

    response = anthropic_client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=400,
        messages=[{'role': 'user', 'content': content}],
    )

    raw_text = response.content[0].text
    result = parse_grade_response(raw_text)
    result['photo_count'] = len(urls_to_use)
    return result


def fetch_redfin_photos(redfin_url: str) -> list[str]:
    """Scrape listing photo URLs from a Redfin property page.

    Returns list of HTTPS image URLs (CDN), empty list on failure.
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        req = urllib.request.Request(redfin_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')

        photo_urls = re.findall(
            r'(https://ssl\.cdn-redfin\.com/photo/[^"\'>\s]+\.(?:jpg|jpeg|webp))',
            html,
            re.IGNORECASE,
        )

        seen = set()
        unique = []
        for url in photo_urls:
            base = re.sub(r'_\d+\.', '.', url)
            if base not in seen:
                seen.add(base)
                unique.append(url)

        return unique[:20]

    except Exception:
        return []
