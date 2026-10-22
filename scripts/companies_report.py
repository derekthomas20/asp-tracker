"""Check data/companies.json against the CMS crosswalk and the saved FDA,
GLEIF and SEC sources, and list what needs the owner's attention.

Prints:
  1. Coverage: for the newest quarter's ASP NDC-HCPCS crosswalk, how many
     HCPCS codes (and how much Medicare Part B spending, newest full year)
     are made by one company, by several, or are not fully assigned yet.
  2. Every labeler code in companies.json that FDA and GLEIF can't confirm,
     or that GLEIF links to a different company.
  3. Ticker problems (a ticker SEC no longer lists, or lists differently).
  4. Labeler names that match two companies' cms_names patterns.
  5. The unassigned labelers touching the most spending.
  6. Rows credited by name to a different company than the code owner
     (for information: e.g. Harrow selling under Novartis's labeler code).

With --issue-file, sections 2-5 (only the parts that need action) are also
written as Markdown for the weekly GitHub issue. The file is empty when
nothing needs action.

To approve a labeler code after checking it (records what FDA and GLEIF say
now; it is flagged again only if that changes):
    .venv\\Scripts\\python scripts/companies_report.py --approve 50242 00004

Usage (from the repository root, after parse.py and fetch_company_sources.py):
    .venv\\Scripts\\python scripts/companies_report.py
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb

from companies import (AGREES, APPROVED, CONFLICT, NAME_MATCH, NDC_5_4_2, NEEDS_REVIEW, ROOT, SKIN_SUBSTITUTE, VERIFIED,
                       approval_record, check_labeler, check_tickers, companies_for_name, credit_row,
                       load_companies,
                       load_fda_labelers, load_gleif, load_sec, save_companies)

DB_PATH = ROOT / "data" / "asp.duckdb"
# Unassigned labelers are put in the GitHub issue only when the HCPCS codes
# they appear on had at least this much Medicare Part B spending.
MIN_SPEND_TO_FLAG = Decimal("5000000")
SHOW_UNASSIGNED = 25


def money(value: Decimal | None) -> str:
    if not value:
        return "$0"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:,.1f}M"
    return f"${value:,.0f}"


def approve(doc: dict, codes: list[str], fda: dict, gleif: dict) -> int:
    today = date.today().isoformat()
    for code in codes:
        cid = doc["_code_owner"].get(code)
        if cid is None:
            print(f"{code}: not in companies.json; add it under a company first.")
            return 1
        company = doc["_by_id"][cid]
        entry = next(e for e in company["labeler_codes"] if e["code"] == code)
        status, detail = check_labeler(doc, company, entry, fda, gleif)
        if status == CONFLICT:
            print(f"{code}: WARNING: {detail}. Approving anyway, as asked.")
        entry["reviewed"] = approval_record(code, fda, gleif, today)
        print(f"{code}: approved as {company['name']} ({detail})")
    save_companies(doc)
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--approve", nargs="+", metavar="CODE", help="approve these labeler codes")
    parser.add_argument("--issue-file", type=Path, help="write the items needing action here as Markdown")
    args = parser.parse_args()

    doc = load_companies()
    fda, gleif, sec = load_fda_labelers(), load_gleif(), load_sec()
    if fda is None or gleif is None:
        print("STOPPED: no FDA or GLEIF snapshot yet. Run scripts/fetch_company_sources.py first.")
        return 1
    if args.approve:
        return approve(doc, args.approve, fda, gleif)

    # --- labeler code checks ------------------------------------------------
    status_of: dict[str, tuple[str, str]] = {}
    for c in doc["companies"]:
        for entry in c["labeler_codes"]:
            status_of[entry["code"]] = check_labeler(doc, c, entry, fda, gleif)
    verified_companies = {doc["_code_owner"][code] for code, (s, _) in status_of.items() if s in VERIFIED}

    ticker_problems = []
    if sec is None:
        ticker_problems.append(("(all)", "no SEC snapshot yet; tickers were not checked"))
    else:
        sec_by_ticker = {r["ticker"]: r for rows in sec.values() for r in rows}
        for c in doc["companies"]:
            for p in check_tickers(c, sec, sec_by_ticker):
                ticker_problems.append((c["name"], p))

    # --- crosswalk coverage -------------------------------------------------
    con = duckdb.connect(str(DB_PATH), read_only=True)
    quarter = con.execute("SELECT max(effective_quarter) FROM crosswalks "
                          "WHERE crosswalk_type = 'ASP' AND is_current").fetchone()[0]
    rows = con.execute("""
        SELECT hcpcs_code, ndc, labeler_name FROM crosswalks
        WHERE crosswalk_type = 'ASP' AND is_current AND effective_quarter = ?
    """, [quarter]).fetchall()
    year = con.execute("SELECT max(data_year) FROM part_b_spending WHERE is_latest_release").fetchone()[0]
    spend = dict(con.execute("""
        SELECT hcpcs_code, sum(total_spending) FROM part_b_spending
        WHERE is_latest_release AND data_year = ? GROUP BY 1
    """, [year]).fetchall())
    cms_names_by_code: dict[str, set[str]] = defaultdict(set)
    for _, ndc, name in rows:
        m = NDC_5_4_2.match(ndc or "")
        if m:
            cms_names_by_code[m.group(1)].add(name)

    makers: dict[str, set[str]] = defaultdict(set)
    incomplete: set[str] = set()
    unassigned: dict[str, dict] = {}   # labeler code (or name, when the NDC has no labeler code) -> details
    overlaps: dict[str, set[str]] = {}
    by_name_moves: dict[tuple, set[str]] = defaultdict(set)
    skipped_skin = set()
    for hcpcs, ndc, name in rows:
        if SKIN_SUBSTITUTE.match(hcpcs):
            skipped_skin.add(hcpcs)
            continue
        m = NDC_5_4_2.match(ndc or "")
        code = m.group(1) if m else None
        cid, reason = credit_row(doc, code, name)
        if cid:
            makers[hcpcs].add(cid)
            owner = doc["_code_owner"].get(code)
            if reason == "name" and owner and owner != cid:
                by_name_moves[(code, owner, cid)].add(name)
        elif reason in ("unassigned", "names overlap"):
            incomplete.add(hcpcs)
            key = code or f"(no labeler code) {name}"
            u = unassigned.setdefault(key, {"names": set(), "hcpcs": set()})
            u["names"].add(name)
            u["hcpcs"].add(hcpcs)
            if reason == "names overlap":
                overlaps[name] = set(companies_for_name(doc, name))

    def credited_verified(hcpcs: str) -> bool:
        return all(c in verified_companies for c in makers[hcpcs])

    buckets = defaultdict(lambda: [0, Decimal(0)])
    all_codes = {r[0] for r in rows if not SKIN_SUBSTITUTE.match(r[0])}
    for hcpcs in all_codes:
        if hcpcs in incomplete:
            b = "not fully assigned yet"
        elif not makers[hcpcs]:
            b = "only distributors/repackagers listed"
        elif len(makers[hcpcs]) == 1:
            b = "one company (verified)" if credited_verified(hcpcs) else "one company (not verified yet)"
        else:
            b = "several companies (multi-source)"
        buckets[b][0] += 1
        buckets[b][1] += spend.get(hcpcs) or 0
    in_year_not_crosswalk = sum((v or 0) for k, v in spend.items() if k not in all_codes)

    # --- print --------------------------------------------------------------
    print(f"Coverage: {quarter} ASP crosswalk, {len(all_codes)} HCPCS codes "
          f"(skin substitutes left out: {len(skipped_skin)}). Spending: Part B {year}.")
    for b in ("one company (verified)", "one company (not verified yet)", "several companies (multi-source)",
              "not fully assigned yet", "only distributors/repackagers listed"):
        n, s = buckets[b]
        print(f"  {b:40} {n:5} codes  {money(s):>10}")
    print(f"  {'codes with spending but not in this crosswalk':40} {'':5}        {money(in_year_not_crosswalk):>10}")

    counts = defaultdict(int)
    for s, _ in status_of.values():
        counts[s] += 1
    print(f"\nLabeler codes in companies.json: {len(status_of)}. "
          + ", ".join(f"{s}: {counts[s]}" for s in (AGREES, NAME_MATCH, APPROVED, NEEDS_REVIEW, CONFLICT)))
    to_check = [(code, s, d) for code, (s, d) in sorted(status_of.items()) if s in (NEEDS_REVIEW, CONFLICT)]
    for code, s, d in to_check:
        print(f"  {code} {doc['_by_id'][doc['_code_owner'][code]]['name']:22} {s.upper()}: {d}")

    print(f"\nTicker problems: {len(ticker_problems) or 'none'}")
    for name, p in ticker_problems:
        print(f"  {name}: {p}")

    if overlaps:
        print("\nLabeler names matching more than one company (fix the cms_names patterns):")
        for name, ids in sorted(overlaps.items()):
            print(f"  {name!r}: {', '.join(sorted(ids))}")

    ranked = sorted(unassigned.items(), key=lambda kv: -sum((spend.get(h) or 0) for h in kv[1]["hcpcs"]))
    print(f"\nUnassigned labelers: {len(unassigned)}. Top {SHOW_UNASSIGNED} by spending on the codes they appear on:")
    for key, u in ranked[:SHOW_UNASSIGNED]:
        s = sum((spend.get(h) or 0) for h in u["hcpcs"])
        print(f"  {key:8} {money(s):>9}  {len(u['hcpcs']):3} codes  {' | '.join(sorted(u['names']))[:90]}")

    if by_name_moves:
        print("\nRows credited by name to a company other than the labeler code's owner:")
        for (code, owner, cid), names in sorted(by_name_moves.items()):
            print(f"  {code} owned by {owner}, credited to {cid}: {' | '.join(sorted(names))[:80]}")

    # --- GitHub issue -------------------------------------------------------
    if args.issue_file:
        flagged = [(k, u) for k, u in ranked
                   if sum((spend.get(h) or 0) for h in u["hcpcs"]) >= MIN_SPEND_TO_FLAG]
        lines = []
        if to_check:
            lines += ["## Labeler codes to check", "",
                      "FDA and GLEIF could not confirm these. If the company is right, approve them "
                      "(they are flagged again only if FDA or GLEIF changes):", "",
                      "```", f"python scripts/companies_report.py --approve {' '.join(c for c, _, _ in to_check)}",
                      "```", "", "If one is wrong, fix it in `data/companies.json` (or ask Claude to).", "",
                      "| Code | In companies.json | Result | What the sources say | Names in the CMS crosswalk |",
                      "|---|---|---|---|---|"]
            for code, s, d in to_check:
                names = " / ".join(sorted(cms_names_by_code.get(code, []))) or "(not in this quarter)"
                lines.append(f"| {code} | {doc['_by_id'][doc['_code_owner'][code]]['name']} | {s} | {d} | {names} |")
            lines.append("")
        if ticker_problems:
            lines += ["## Tickers", ""] + [f"- **{n}**: {p}" for n, p in ticker_problems] + [""]
        if overlaps:
            lines += ["## Names that match two companies", "",
                      "Change the `cms_names` patterns so each name matches one company:", ""]
            lines += [f"- `{n}`: {', '.join(sorted(ids))}" for n, ids in sorted(overlaps.items())] + [""]
        if flagged:
            lines += [f"## Unassigned labelers ({quarter} crosswalk)", "",
                      f"Not in `data/companies.json` yet, on HCPCS codes with at least {money(MIN_SPEND_TO_FLAG)} "
                      f"of Part B spending in {year}. Their codes show as unassigned on the site until added.", "",
                      "| Labeler code | Names in the CMS crosswalk | HCPCS codes | Spending on those codes |",
                      "|---|---|---|---|"]
            for key, u in flagged:
                s = sum((spend.get(h) or 0) for h in u["hcpcs"])
                lines.append(f"| {key} | {' / '.join(sorted(u['names']))} | {', '.join(sorted(u['hcpcs']))[:60]} | {money(s)} |")
            lines.append("")
        body = "\n".join(lines)
        if body:
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
            body = ("Written by `scripts/companies_report.py` in the weekly update. "
                    "It is updated each week and closed when nothing is left.\n\n"
                    + body + f"\n<!-- content-hash: {digest} -->\n")
        args.issue_file.write_text(body, encoding="utf-8")
        print(f"\nIssue text: {args.issue_file} ({'nothing needs action' if not body else f'{len(lines)} lines'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
