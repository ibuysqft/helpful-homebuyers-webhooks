#!/usr/bin/env python3
"""
import_buyers.py — Upsert cash buyers from CSV into Supabase.

Usage:
    python scripts/import_buyers.py buyers.csv [--dry-run]

CSV columns (header row required):
    first_name, last_name, email, phone, company,
    price_range_min, price_range_max,
    preferred_states,   (pipe-separated: CA|TX|FL)
    property_types,     (pipe-separated: multifamily|retail)
    notes
"""
import argparse
import csv
import os
import sys

# Allow running from any directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


REQUIRED_COLUMNS = {
    "first_name", "last_name", "email", "phone", "company",
    "price_range_min", "price_range_max",
    "preferred_states", "property_types", "notes",
}


def parse_row(row: dict) -> dict:
    preferred_states = [s.strip() for s in row.get("preferred_states", "").split("|") if s.strip()]
    property_types   = "|".join(pt.strip() for pt in row.get("property_types", "").split("|") if pt.strip())

    return {
        "first_name":      row.get("first_name", "").strip(),
        "last_name":       row.get("last_name", "").strip(),
        "email":           row.get("email", "").strip().lower(),
        "phone":           row.get("phone", "").strip(),
        "company":         row.get("company", "").strip(),
        "price_range_min": float(row.get("price_range_min") or 0),
        "price_range_max": float(row.get("price_range_max") or 0),
        "preferred_states": preferred_states,
        "buy_criteria":    {"property_type": property_types},
        "notes":           row.get("notes", "").strip(),
        "status":          "active",
    }


def import_csv(csv_path: str, dry_run: bool = False) -> dict:
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

    if not dry_run and (not supabase_url or not supabase_key):
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set", file=sys.stderr)
        sys.exit(1)

    sb = None
    if not dry_run:
        from supabase import create_client
        sb = create_client(supabase_url, supabase_key)

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            print(f"ERROR: CSV missing columns: {', '.join(sorted(missing))}", file=sys.stderr)
            sys.exit(1)

        rows = [parse_row(r) for r in reader]

    print(f"{'[DRY RUN] ' if dry_run else ''}Importing {len(rows)} buyer(s) from {csv_path}...")

    inserted = updated = errors = 0

    for record in rows:
        email = record.get("email", "")
        if not email:
            print(f"  SKIP: no email for {record.get('first_name')} {record.get('last_name')}")
            continue

        if dry_run:
            print(f"  WOULD UPSERT: {email} | {record['first_name']} {record['last_name']}")
            inserted += 1
            continue

        try:
            result = (
                sb.table("cash_buyers")
                .upsert(record, on_conflict="email")
                .execute()
            )
            if result.data:
                print(f"  OK: {email}")
                inserted += 1
            else:
                print(f"  ERROR: {email} — no data returned", file=sys.stderr)
                errors += 1
        except Exception as exc:
            print(f"  ERROR: {email} — {exc}", file=sys.stderr)
            errors += 1

    print(f"\nDone. upserted={inserted} errors={errors}")
    return {"upserted": inserted, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description="Import cash buyers from CSV into Supabase")
    parser.add_argument("csv_file", help="Path to CSV file")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    import_csv(args.csv_file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
