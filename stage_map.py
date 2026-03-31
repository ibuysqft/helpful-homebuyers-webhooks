"""STAGE_MAP — Retell call outcomes → GHL pipeline stage names.

Kept in a separate module so it can be imported by tests without
pulling in the full FastAPI application stack.
"""

STAGE_MAP: dict[str, str] = {
    # Generic outcomes (used by Shelby, Alex, Cole, Jordan)
    "Appointment Set":        "AI - Qualified (Appt Set)",
    "Attorney Intro Agreed":  "AI - Qualified (Appt Set)",
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
}
