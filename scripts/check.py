"""Quick look at data/asp.duckdb so it can be checked against the CMS files.

Prints:
  1. Payment limit row counts for every quarter (current version, and all
     versions if CMS posted more than one).
  2. Five random rows from the latest quarter, with the file and line number
     each came from, so you can open the CMS CSV and compare by hand.
  3. Any HCPCS code in data/ncr_markets.json (the NCR calculator's markets)
     that is missing from the latest quarter.

Usage (from the repository root):
    .venv\\Scripts\\python scripts/check.py
    .venv\\Scripts\\python scripts/check.py --seed 42   # same 5 rows every time
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "asp.duckdb"
NCR_MARKETS = ROOT / "data" / "ncr_markets.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, help="pick the same random rows each time")
    parser.add_argument("--rows", type=int, default=5, help="how many random rows to show")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not DB_PATH.exists():
        print(f"{DB_PATH.relative_to(ROOT)} not found. Run scripts/parse.py first.")
        return 1

    con = duckdb.connect(str(DB_PATH), read_only=True)

    print("Payment limit rows per quarter")
    print(f"  {'Quarter':<9} {'Current':>8} {'Versions':>9} {'All rows':>9}  Current file posted")
    counts = con.execute("""
        SELECT effective_quarter,
               count(*) FILTER (WHERE is_current)      AS current_rows,
               count(DISTINCT file_version)            AS versions,
               count(*)                                AS all_rows,
               max(cms_posted_date) FILTER (WHERE is_current) AS posted
        FROM payment_limits
        GROUP BY effective_quarter
        ORDER BY effective_quarter
    """).fetchall()
    for quarter, current, versions, total, posted in counts:
        print(f"  {quarter:<9} {current:>8,} {versions:>9} {total:>9,}  {posted or 'not on CMS page'}")

    latest = counts[-1][0]
    rows = con.execute("""
        SELECT hcpcs_code, short_description, hcpcs_dosage, payment_limit_text,
               source_file, source_member, source_row
        FROM payment_limits
        WHERE effective_quarter = ? AND is_current
        ORDER BY source_row
    """, [latest]).fetchall()
    latest_codes = {r[0] for r in con.execute(
        "SELECT DISTINCT hcpcs_code FROM payment_limits WHERE effective_quarter = ? AND is_current",
        [latest]).fetchall()}
    con.close()

    picks = sorted(random.Random(args.seed).sample(rows, min(args.rows, len(rows))), key=lambda r: r[6])
    print(f"\n{len(picks)} random rows from {latest} (current version)")
    print(f"  From: {picks[0][4]}")
    print(f"        -> {picks[0][5]}\n")
    print(f"  {'Line':>5}  {'HCPCS':<6} {'Short description':<30} {'Dosage':<14} Payment limit")
    for code, desc, dosage, limit, _, _, line in picks:
        print(f"  {line:>5}  {code:<6} {desc[:30]:<30} {(dosage or '')[:14]:<14} {limit}")
    print("\n'Line' is the line number in the CSV. Open the CSV in a text editor or Excel")
    print("and check that HCPCS code, description, dosage and payment limit match exactly.")

    with NCR_MARKETS.open(encoding="utf-8") as f:
        markets = json.load(f)["markets"]
    configured = [(m["id"], p["name"], p["hcpcs"]) for m in markets for p in m["products"]]
    missing = [(mid, name, code) for mid, name, code in configured if code and code not in latest_codes]
    no_code = [(mid, name) for mid, name, code in configured if not code]
    print(f"\nNCR calculator codes ({NCR_MARKETS.relative_to(ROOT)}) checked against {latest}")
    print(f"  {len(configured) - len(no_code) - len(missing)} of {len(configured) - len(no_code)} products with a code are in the file")
    for mid, name, code in missing:
        print(f"  MISSING  {code:<6} {name} ({mid})")
    for mid, name in no_code:
        print(f"  no code  {name} ({mid}); visitors type its limit")
    if not missing:
        print("  No configured code is missing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
