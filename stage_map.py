"""STAGE_MAP — Retell call outcomes → GHL pipeline stage names.

Kept in a separate module so it can be imported by tests without
pulling in the full FastAPI application stack.
"""

STAGE_MAP: dict[str, str] = {
    # Generic outcomes (used by Shelby, Alex, Cole, Jordan)
    "Appointment Set":        "AI - Qualified (Appt Set)",
    "Attorney Intro Agreed":  "AI - Qualified (Appt Set)",
    "Needs Human Offer Review": "AI - Qualified (Offer Review)",
    "Short Sale Review":        "AI - Short Sale Review",
    "Cash Offer Ready":         "AI - Cash Offer",
    "Novation Review":          "AI - Novation Review",
    "Seeds Planted":          "AI - Qualified (Seeds Planted)",
    "Micro-Commitment":       "AI - Qualified (Seeds Planted)",
    "Call Back Later":        "AI - Qualified (Seeds Planted)",
    "Interested - Reviewing": "AI - Qualified (Seeds Planted)",
    "Not Ready":              "AI - Qualified (Seeds Planted)",
    "Voicemail":              "AI - No Answer",
    "No Answer":              "AI - No Answer",
    "Not Interested":         "Dead - Not Interested",
    # MLS / Marcus outcomes — mapped to On Market Offers pipeline stages
    "interested_write_offer":   "AI - Qualified (Offer Submitted)",
    "Interested - Write Offer": "AI - Qualified (Offer Submitted)",
    "in_escrow_backup":         "AI - Qualified (Seeds Planted)",
    "In Escrow - Backup":       "AI - Qualified (Seeds Planted)",
    "not_interested":           "Dead",
    "Disqualified":             "Dead",
    "DQ - Not Heir":            "Dead",
    "DQ - Already Sold":        "Dead",
    "DQ - Active Litigation":   "Dead",
    "Wrong Number":             "Dead",
    "Disconnected":             "Dead",
    # Unified pipeline stages (HHB Wholesale Master)
    "Consent Check":           "Consent Check",
    "AI SMS Active":           "AI SMS Active",
    "AI Call Queued":          "AI Call Queued",
    "Human Follow-Up":         "Human Follow-Up",
    "Offer Needed":            "Offer Needed",
    "Offer Sent":              "Offer Sent",
    "Under Contract":          "Under Contract",
    "Dispo Blast Sent":        "Dispo Blast Sent",
    "Buyer Interested":        "Buyer Interested",
    "Buyer Qualified":         "Buyer Qualified",
    "Wrap Terms Set":          "Wrap Terms Set",
    "Wrap Buyer Found":        "Wrap Buyer Found",
    "Wrap Originated":         "Wrap Originated",
    "Novation Signed":         "Novation Signed",
    "Listed on MLS":           "Listed on MLS",
    "Offer Accepted":          "Offer Accepted",
    "Closed Won":              "Closed Won",
    "Long-Term Nurture":       "Long-Term Nurture",
}
