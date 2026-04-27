"""
One-time script: apply Bouncer CSV export results to the prospects table.

Reads all CSV files from logs/, applies the status mapping, and updates
email_validation_status + email_validated_at for matching prospects.
Does NOT re-call the Bouncer API — uses the already-paid-for CSV data.

Status mapping (mirrors bouncer.py):
  deliverable + acceptAll=no  + toxicity 0-5  → valid
  deliverable + acceptAll=yes                  → catch-all
  deliverable + toxicity > 5                   → catch-all
  risky                                        → catch-all
  undeliverable                                → invalid
  unknown                                      → unknown

Run:
  railway run python scripts/apply_bouncer_logs.py
  -- or locally with DATABASE_URL set --
  python scripts/apply_bouncer_logs.py
"""

import csv
import glob
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.models.prospect import Prospect

LOGS_GLOB = str(Path(__file__).parent.parent / "logs" / "*.csv")


def map_status(status: str, accept_all: str, toxicity: int) -> str:
    status = status.strip().lower()
    accept_all = accept_all.strip().lower()
    if status == "deliverable":
        if accept_all == "yes" or toxicity > 5:
            return "catch-all"
        return "valid"
    if status == "risky":
        return "catch-all"
    if status == "undeliverable":
        return "invalid"
    return "unknown"


def load_csv_results() -> dict[str, str]:
    """Read all CSVs and return {email_lower: mapped_status}. Later files win on duplicates."""
    results: dict[str, str] = {}
    files = sorted(glob.glob(LOGS_GLOB))
    if not files:
        print(f"No CSV files found at {LOGS_GLOB}")
        sys.exit(1)

    for path in files:
        with open(path, encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                email = (row.get("email") or "").strip().lower()
                if not email:
                    continue
                status = map_status(
                    row.get("status", "unknown"),
                    row.get("acceptAll", "no"),
                    int(row.get("toxicity") or 0),
                )
                results[email] = status

    print(f"Loaded {len(results)} unique emails from {len(files)} CSV files")
    return results


def main():
    bouncer_results = load_csv_results()

    from collections import Counter
    dist = Counter(bouncer_results.values())
    print(f"  valid={dist['valid']}  catch-all={dist['catch-all']}  "
          f"invalid={dist['invalid']}  unknown={dist['unknown']}")

    db = SessionLocal()
    try:
        prospects = db.query(Prospect).all()
        print(f"\nMatching against {len(prospects)} prospects in DB...")

        now = datetime.now(timezone.utc)
        updated = 0
        skipped = 0
        not_found = 0

        for prospect in prospects:
            email = (prospect.email or "").strip().lower()
            if email not in bouncer_results:
                not_found += 1
                continue
            new_status = bouncer_results[email]
            if prospect.email_validation_status == new_status:
                skipped += 1
                continue
            prospect.email_validation_status = new_status
            prospect.email_validated_at = now
            updated += 1

        db.commit()
        print(f"  Updated : {updated}")
        print(f"  Skipped (no change): {skipped}")
        print(f"  Not in CSV: {not_found}")
        print("Done.")

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
