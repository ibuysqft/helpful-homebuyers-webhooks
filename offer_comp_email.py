"""
Offer + Comp Package Email
Fires automatically when we submit a formal offer on an MLS property.

Usage:
    from offer_comp_email import send_offer_comp_email

    send_offer_comp_email(
        contact_id="ghl_contact_id",
        ghl_headers={...},
        data={
            "address": "123 Main St, Antioch CA 94509",
            "offer_price": 285000,
            "arv": 340000,
            "repair_estimate": 25000,
            "list_price": 320000,
            "days_on_market": 47,
            "agent_name": "John Smith",
            "agent_phone": "925-555-1234",
            "comps": [
                {"address": "110 Oak Ave", "sqft": 1200, "bed": 3, "bath": 2,
                 "sale_price": 348000, "sold_date": "2026-02-12", "price_sqft": 290},
                ...
            ],
            "contact_name": "Sarah Johnson",
            "contact_email": "sarah@email.com",   # optional override
        }
    )
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

log = logging.getLogger(__name__)

GHL_BASE = "https://services.leadconnectorhq.com"


def _fmt_currency(value: float | int) -> str:
    return f"${value:,.0f}"


def _rule_90_mao(arv: float, repairs: float) -> float:
    """Maximum Allowable Offer: ARV × 0.90 − repairs."""
    return arv * 0.90 - repairs


def _build_comp_rows(comps: list[dict]) -> str:
    if not comps:
        return '<tr><td colspan="6" style="text-align:center;color:#6b7280;padding:16px;">No comps provided</td></tr>'
    rows = []
    for c in comps:
        rows.append(f"""
        <tr>
          <td style="{_td}">{c.get("address","—")}</td>
          <td style="{_td_c}">{c.get("bed","—")}/{c.get("bath","—")}</td>
          <td style="{_td_c}">{c.get("sqft","—"):,}</td>
          <td style="{_td_r}">{_fmt_currency(c.get("sale_price", 0))}</td>
          <td style="{_td_r}">${c.get("price_sqft", 0):,.0f}/sf</td>
          <td style="{_td_c}">{c.get("sold_date","—")}</td>
        </tr>""")
    return "\n".join(rows)


# Cell styles (inline for email client compat)
_td = "padding:10px 12px;border-bottom:1px solid #e5e7eb;color:#374151;font-size:13px;"
_td_c = _td + "text-align:center;"
_td_r = _td + "text-align:right;"
_th = ("padding:10px 12px;background:#1e293b;color:#f9fafb;font-size:12px;"
       "font-weight:600;text-transform:uppercase;letter-spacing:.05em;")
_th_c = _th + "text-align:center;"
_th_r = _th + "text-align:right;"


def build_offer_comp_html(data: dict) -> str:
    """Return full HTML email body for the Offer + Comp Package."""
    address = data.get("address", "Subject Property")
    offer_price = float(data.get("offer_price", 0))
    arv = float(data.get("arv", 0))
    repairs = float(data.get("repair_estimate", 0))
    list_price = float(data.get("list_price", 0))
    dom = data.get("days_on_market", "—")
    agent_name = data.get("agent_name", "")
    agent_phone = data.get("agent_phone", "")
    comps = data.get("comps", [])
    contact_name = data.get("contact_name", "there")
    first_name = contact_name.split()[0] if contact_name else "there"

    mao = _rule_90_mao(arv, repairs) if arv else 0
    offer_vs_arv = (offer_price / arv * 100) if arv else 0
    offer_vs_list = ((offer_price / list_price - 1) * 100) if list_price else 0
    list_vs_arv = (list_price / arv * 100) if arv else 0
    vs_list_label = f"{abs(offer_vs_list):.1f}% {'below' if offer_vs_list < 0 else 'above'} list"

    comp_rows = _build_comp_rows(comps)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Offer + Comp Package — {address}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">

<!-- Wrapper -->
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 16px;">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">

  <!-- Header -->
  <tr>
    <td style="background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);padding:32px 40px;">
      <p style="margin:0 0 4px;color:#94a3b8;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;">Helpful Home Buyers USA</p>
      <h1 style="margin:0 0 8px;color:#f8fafc;font-size:24px;font-weight:700;line-height:1.2;">Offer + Comp Package</h1>
      <p style="margin:0;color:#cbd5e1;font-size:14px;">{address}</p>
    </td>
  </tr>

  <!-- Greeting -->
  <tr>
    <td style="padding:32px 40px 8px;">
      <p style="margin:0 0 12px;color:#374151;font-size:15px;line-height:1.6;">
        Hi {first_name},
      </p>
      <p style="margin:0;color:#374151;font-size:15px;line-height:1.6;">
        We've submitted our formal offer on <strong>{address}</strong>. Below is the full comp analysis
        and offer breakdown that supports our price — fully transparent, nothing hidden.
      </p>
    </td>
  </tr>

  <!-- Offer Summary Cards -->
  <tr>
    <td style="padding:24px 40px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="48%" style="background:#0f172a;border-radius:10px;padding:20px 24px;vertical-align:top;">
            <p style="margin:0 0 4px;color:#94a3b8;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;">Our Offer</p>
            <p style="margin:0;color:#10b981;font-size:28px;font-weight:700;">{_fmt_currency(offer_price)}</p>
            <p style="margin:4px 0 0;color:#64748b;font-size:12px;">{vs_list_label} price</p>
          </td>
          <td width="4%"></td>
          <td width="48%" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:20px 24px;vertical-align:top;">
            <p style="margin:0 0 8px;color:#6b7280;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;">Key Numbers</p>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="color:#374151;font-size:13px;padding-bottom:6px;">List Price</td>
                <td style="color:#374151;font-size:13px;font-weight:600;text-align:right;padding-bottom:6px;">{_fmt_currency(list_price)}</td>
              </tr>
              <tr>
                <td style="color:#374151;font-size:13px;padding-bottom:6px;">ARV (After Repairs)</td>
                <td style="color:#374151;font-size:13px;font-weight:600;text-align:right;padding-bottom:6px;">{_fmt_currency(arv)}</td>
              </tr>
              <tr>
                <td style="color:#374151;font-size:13px;padding-bottom:6px;">Repair Estimate</td>
                <td style="color:#e85d5d;font-size:13px;font-weight:600;text-align:right;padding-bottom:6px;">({_fmt_currency(repairs)})</td>
              </tr>
              <tr>
                <td style="color:#374151;font-size:13px;">Days on Market</td>
                <td style="color:#374151;font-size:13px;font-weight:600;text-align:right;">{dom} days</td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- 90% Rule Breakdown -->
  <tr>
    <td style="padding:0 40px 24px;">
      <div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:10px;padding:20px 24px;">
        <p style="margin:0 0 12px;color:#92400e;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;">📐 Our Offer Math (90% Rule)</p>
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:#78350f;font-size:13px;padding-bottom:6px;">ARV × 90%</td>
            <td style="color:#78350f;font-size:13px;font-weight:600;text-align:right;padding-bottom:6px;">{_fmt_currency(arv * 0.9)}</td>
          </tr>
          <tr>
            <td style="color:#78350f;font-size:13px;padding-bottom:6px;">− Repair Estimate</td>
            <td style="color:#78350f;font-size:13px;font-weight:600;text-align:right;padding-bottom:6px;">({_fmt_currency(repairs)})</td>
          </tr>
          <tr style="border-top:1px solid #fcd34d;">
            <td style="color:#92400e;font-size:14px;font-weight:700;padding-top:8px;">Maximum Allowable Offer (MAO)</td>
            <td style="color:#92400e;font-size:14px;font-weight:700;text-align:right;padding-top:8px;">{_fmt_currency(mao)}</td>
          </tr>
        </table>
        <p style="margin:12px 0 0;color:#78350f;font-size:12px;line-height:1.5;">
          Our offer of <strong>{_fmt_currency(offer_price)}</strong> is {f"within our MAO — we have room to work." if offer_price <= mao else f"{_fmt_currency(offer_price - mao)} above MAO — aggressive offer, minimal room for negotiation."}
          This gives us the margin to absorb repairs and sell at or near ARV.
        </p>
      </div>
    </td>
  </tr>

  <!-- Comp Table -->
  <tr>
    <td style="padding:0 40px 24px;">
      <p style="margin:0 0 12px;color:#111827;font-size:16px;font-weight:700;">Comparable Sales</p>
      <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        <thead>
          <tr>
            <th style="{_th}">Address</th>
            <th style="{_th_c}">Bed/Bath</th>
            <th style="{_th_c}">SqFt</th>
            <th style="{_th_r}">Sale Price</th>
            <th style="{_th_r}">$/SqFt</th>
            <th style="{_th_c}">Sold</th>
          </tr>
        </thead>
        <tbody>
          {comp_rows}
        </tbody>
      </table>
      <p style="margin:10px 0 0;color:#6b7280;font-size:12px;">
        * Comps pulled within 0.5mi radius, similar bed/bath/sqft, sold within 90 days.
        ARV of <strong>{_fmt_currency(arv)}</strong> reflects conservative median of above sales.
      </p>
    </td>
  </tr>

  <!-- Offer Position vs ARV -->
  {"" if not arv else f"""
  <tr>
    <td style="padding:0 40px 24px;">
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:20px 24px;">
        <p style="margin:0 0 8px;color:#166534;font-size:13px;font-weight:700;">Offer Position</p>
        <p style="margin:0;color:#166534;font-size:13px;line-height:1.6;">
          Our offer of <strong>{_fmt_currency(offer_price)}</strong> represents <strong>{offer_vs_arv:.1f}% of ARV</strong>.
          The listed price is {list_vs_arv:.1f}% of ARV — {"overpriced relative to comps, which gives us negotiating room." if list_vs_arv > 90 else "already at a reasonable discount, reflected in our offer."}
        </p>
      </div>
    </td>
  </tr>
  """}

  <!-- Agent Info (if provided) -->
  {"" if not agent_name else f"""
  <tr>
    <td style="padding:0 40px 24px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:20px 24px;">
        <p style="margin:0 0 8px;color:#374151;font-size:13px;font-weight:700;">Listing Agent</p>
        <p style="margin:0;color:#374151;font-size:14px;">{agent_name}</p>
        {"" if not agent_phone else f'<p style="margin:4px 0 0;color:#6b7280;font-size:13px;">{agent_phone}</p>'}
      </div>
    </td>
  </tr>
  """}

  <!-- CTA -->
  <tr>
    <td style="padding:0 40px 32px;">
      <p style="margin:0 0 16px;color:#374151;font-size:15px;line-height:1.6;">
        Any questions about the numbers? We're happy to walk you through the comp analysis in detail.
        Just reply to this email or call us directly.
      </p>
      <table cellpadding="0" cellspacing="0">
        <tr>
          <td style="background:#10b981;border-radius:8px;padding:14px 28px;">
            <a href="tel:+17039401159" style="color:#ffffff;font-size:14px;font-weight:700;text-decoration:none;">Call Us: (703) 940-1159</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:24px 40px;">
      <p style="margin:0;color:#9ca3af;font-size:12px;line-height:1.6;">
        <strong style="color:#6b7280;">Helpful Home Buyers USA</strong><br>
        We buy homes as-is for cash — no repairs, no agent fees, no surprises.<br>
        <a href="https://helpfulhomebuyersusa.com" style="color:#10b981;">helpfulhomebuyersusa.com</a>
        &nbsp;|&nbsp; (703) 940-1159
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>

</body>
</html>"""


def send_offer_comp_email(
    contact_id: str,
    ghl_headers: dict,
    data: dict,
) -> bool:
    """
    Send Offer + Comp Package email to a contact via GHL conversations API.

    Args:
        contact_id: GHL contact ID
        ghl_headers: Authorization headers for GHL API
        data: Dict with offer/comp data (see module docstring)

    Returns:
        True on success
    """
    # Resolve email from data or fetch from GHL
    to_email = data.get("contact_email", "")
    if not to_email:
        r = requests.get(
            f"{GHL_BASE}/contacts/{contact_id}",
            headers=ghl_headers,
            timeout=10,
        )
        if r.status_code == 200:
            contact = r.json().get("contact", r.json())
            to_email = contact.get("email", "")
        if not to_email:
            log.warning("offer_comp_email: no email on contact %s — skipping", contact_id)
            return False

    address = data.get("address", "Subject Property")
    html_body = build_offer_comp_html(data)

    payload = {
        "type": "Email",
        "contactId": contact_id,
        "emailTo": to_email,
        "subject": f"Your Offer Package — {address}",
        "html": html_body,
    }

    r = requests.post(
        f"{GHL_BASE}/conversations/messages",
        headers=ghl_headers,
        json=payload,
        timeout=15,
    )
    success = r is not None and r.status_code in (200, 201)
    if success:
        log.info("offer_comp_email sent to %s for contact %s", to_email, contact_id)
    else:
        log.error(
            "offer_comp_email FAILED contact=%s status=%s body=%s",
            contact_id,
            r.status_code if r else "none",
            r.text[:200] if r else "",
        )
    return success
