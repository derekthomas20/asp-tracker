"""Build the public website in public/ from data/asp.duckdb.

Every number on the site is read from the database by this script, and the
database is built from the CMS files in raw/ by scripts/parse.py. Nothing is
typed in by hand.

Pages written to public/:
    index.html          the latest quarter in one paragraph
    movers.html         biggest increases and decreases vs the prior quarter
    changes.html        codes new or dropped this quarter
    codes/XXXXX.html    one page per HCPCS code: chart, table, CMS source links
    search.html         find a code by code or drug name
    downloads.html      the full history as one CSV (in downloads/)
    about.html          where the data comes from and what it means

Templates are in site/templates and the stylesheet is site/style.css.
public/ is deleted and rebuilt from scratch on every run.

Usage (from the repository root, after scripts/parse.py):
    .venv\\Scripts\\python scripts/build_site.py
"""

from __future__ import annotations

import csv
import html
import json
import re
import shutil
import statistics
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "asp.duckdb"
MANIFEST_PATH = ROOT / "data" / "manifest.json"
TEMPLATES = ROOT / "site" / "templates"
STYLESHEET = ROOT / "site" / "style.css"
OUT = ROOT / "public"
CSV_NAME = "asp-payment-limits-history.csv"

TOP_N = 25  # rows in each movers table

NAV = [
    ("index.html", "Home"),
    ("movers.html", "Movers"),
    ("changes.html", "New & dropped"),
    ("search.html", "Search"),
    ("downloads.html", "Downloads"),
    ("about.html", "About"),
]

QUARTER_MONTHS = {"1": ("January", "March"), "2": ("April", "June"),
                  "3": ("July", "September"), "4": ("October", "December")}


# --------------------------------------------------------------------------
# Display helpers. These change how values look on the page, never the
# values themselves, and never the database.
# --------------------------------------------------------------------------

# Only complete HTML entities such as "&sol;" or "&#47;" are decoded. CMS also
# uses "&" as a plain abbreviation: 90377 "Rabies ig ht&sol human im/sc" means
# "heat & solvent", so a bare "&sol" with no semicolon is left alone.
ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def display_text(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(ENTITY.sub(lambda m: html.unescape(m.group(0)), text).split())


def money(value: Decimal | None) -> str:
    """Exact dollar amount: commas, at least 2 decimal places, and every
    decimal place CMS gave (no rounding). None -> 'N/A'."""
    if value is None:
        return "N/A"
    places = max(2, -value.normalize().as_tuple().exponent)
    return f"${value:,.{places}f}"


def sign(value) -> str:
    return "+" if value > 0 else "−" if value < 0 else ""


def signed_money(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return sign(value) + money(abs(value))


def percent(value: Decimal | None) -> str:
    """Percent change, shown to one decimal place (the exact change in
    dollars is always shown next to it)."""
    if value is None:
        return "N/A"
    return f"{sign(value)}{abs(value):.1f}%"


def number(value: int) -> str:
    return f"{value:,}"


def quarter_label(q: str) -> str:
    return f"Q{q[5]} {q[:4]}"


def quarter_months(q: str) -> str:
    first, last = QUARTER_MONTHS[q[5]]
    return f"{first}–{last} {q[:4]}"


def quarter_offset(q: str, n: int) -> str:
    """The quarter n quarters after q (n may be negative)."""
    index = int(q[:4]) * 4 + int(q[5]) - 1 + n
    return f"{index // 4}Q{index % 4 + 1}"


def long_date(value: date | None) -> str:
    if value is None:
        return "not listed"
    return f"{value:%B} {value.day}, {value.year}"


def same_unit(a: str | None, b: str | None) -> bool:
    """Billing units are compared ignoring case and spacing ('1 MG' = '1 mg')."""
    return " ".join((a or "").upper().split()) == " ".join((b or "").upper().split())


def plain_decimal(value: Decimal | None) -> str:
    """'12.345000' -> '12.345' for the CSV. Same value, no trailing zeros."""
    return "" if value is None else f"{value.normalize():f}"


# --------------------------------------------------------------------------
# Reading the data
# --------------------------------------------------------------------------

def load_rows(con) -> list[dict]:
    cur = con.execute("""
        SELECT effective_quarter, hcpcs_code, short_description, hcpcs_dosage,
               payment_limit, payment_limit_text, source_file, cms_posted_date,
               file_version, is_current
        FROM payment_limits
        ORDER BY effective_quarter, hcpcs_code, file_version
    """)
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    bad = sorted({r["hcpcs_code"] for r in rows if not re.fullmatch(r"[A-Z0-9]{5}", r["hcpcs_code"])})
    if bad:
        # Codes become file names, so anything unexpected stops the build.
        raise SystemExit(f"STOPPED: unexpected HCPCS code format: {bad[:10]}")
    return rows


def compare(latest: dict[str, dict], prior: dict[str, dict]) -> dict:
    """Compare each code in the latest quarter with the prior quarter.

    A code is compared only when it is in both quarters, has a payment limit
    in both, the billing unit did not change, and the prior limit is not
    zero. Everything else is counted so the site can say what was left out.
    """
    compared, no_limit, unit_changed, zero_prior = [], [], [], []
    for code, cur in latest.items():
        prev = prior.get(code)
        if prev is None:
            continue
        if cur["payment_limit"] is None or prev["payment_limit"] is None:
            no_limit.append(code)
        elif not same_unit(cur["hcpcs_dosage"], prev["hcpcs_dosage"]):
            unit_changed.append(code)
        elif prev["payment_limit"] == 0:
            zero_prior.append(code)
        else:
            change = cur["payment_limit"] - prev["payment_limit"]
            compared.append({
                "code": code,
                "description": display_text(cur["short_description"]),
                "dosage": cur["hcpcs_dosage"],
                "prior": prev["payment_limit"],
                "latest": cur["payment_limit"],
                "change": change,
                "pct": change / prev["payment_limit"] * 100,
            })
    return {"compared": compared, "no_limit": no_limit,
            "unit_changed": unit_changed, "zero_prior": zero_prior}


# --------------------------------------------------------------------------
# Writing the site
# --------------------------------------------------------------------------

def make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,  # a typo in a template stops the build
        trim_blocks=True,
        lstrip_blocks=True,
        extensions=["jinja2.ext.do"],  # lets templates build short lists
    )
    env.filters.update(money=money, signed_money=signed_money, percent=percent,
                       num=number, quarter=quarter_label, months=quarter_months,
                       long_date=long_date, desc=display_text)
    return env


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not DB_PATH.exists():
        print(f"{DB_PATH.relative_to(ROOT)} not found. Run scripts/parse.py first.")
        return 1

    with MANIFEST_PATH.open(encoding="utf-8") as f:
        manifest = json.load(f)
    cms_url = {e["local_path"]: e["url"] for e in manifest["files"]}
    last_download = max(datetime.fromisoformat(e["downloaded_at"]) for e in manifest["files"])

    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = load_rows(con)
    con.close()

    quarters = sorted({r["effective_quarter"] for r in rows})
    if len(quarters) < 2:
        raise SystemExit("STOPPED: need at least two quarters of data to compare.")
    latest_q, prior_q = quarters[-1], quarters[-2]

    current = [r for r in rows if r["is_current"]]
    by_quarter: dict[str, dict[str, dict]] = {}
    for r in current:
        by_quarter.setdefault(r["effective_quarter"], {})[r["hcpcs_code"]] = r
    latest, prior = by_quarter[latest_q], by_quarter[prior_q]

    posted = {r["cms_posted_date"] for r in latest.values()}
    if len(posted) != 1:
        raise SystemExit(f"STOPPED: {latest_q} current rows have {len(posted)} posted dates.")

    # ---- Comparison with the prior quarter --------------------------------
    cmp = compare(latest, prior)
    compared = cmp["compared"]
    increases = sorted((c for c in compared if c["change"] > 0), key=lambda c: -c["pct"])[:TOP_N]
    decreases = sorted((c for c in compared if c["change"] < 0), key=lambda c: c["pct"])[:TOP_N]

    def code_row(r: dict) -> dict:
        return {"code": r["hcpcs_code"], "description": display_text(r["short_description"]),
                "dosage": r["hcpcs_dosage"], "limit": r["payment_limit"]}

    new_codes = [code_row(latest[c]) for c in sorted(latest.keys() - prior.keys())]
    dropped_codes = [code_row(prior[c]) for c in sorted(prior.keys() - latest.keys())]

    summary = {
        "latest_q": latest_q,
        "prior_q": prior_q,
        "posted": posted.pop(),
        "n_codes": len(latest),
        "n_no_limit": sum(1 for r in latest.values() if r["payment_limit"] is None),
        "compared": len(compared),
        "up": sum(1 for c in compared if c["change"] > 0),
        "down": sum(1 for c in compared if c["change"] < 0),
        "same": sum(1 for c in compared if c["change"] == 0),
        "median_pct": statistics.median(c["pct"] for c in compared) if compared else None,
        "no_limit": len(cmp["no_limit"]),
        "unit_changed": len(cmp["unit_changed"]),
        "zero_prior": len(cmp["zero_prior"]),
        "n_new": len(new_codes),
        "n_dropped": len(dropped_codes),
    }

    # ---- One page per code -------------------------------------------------
    rows_by_code: dict[str, list[dict]] = {}
    for r in rows:
        rows_by_code.setdefault(r["hcpcs_code"], []).append(r)

    code_pages = []
    for code, code_rows in sorted(rows_by_code.items()):
        cur_rows = {r["effective_quarter"]: r for r in code_rows if r["is_current"]}
        newest = cur_rows[max(cur_rows)]
        history = []
        for r in sorted(code_rows, key=lambda r: (r["effective_quarter"], r["file_version"]), reverse=True):
            prev = cur_rows.get(quarter_offset(r["effective_quarter"], -1))
            change = None
            if (r["is_current"] and prev and r["payment_limit"] is not None
                    and prev["payment_limit"] is not None
                    and same_unit(r["hcpcs_dosage"], prev["hcpcs_dosage"])):
                change = r["payment_limit"] - prev["payment_limit"]
            history.append({
                "quarter": r["effective_quarter"],
                "description": display_text(r["short_description"]),
                "dosage": r["hcpcs_dosage"],
                "limit": r["payment_limit"],
                "change": change,
                "is_current": r["is_current"],
                "version": r["file_version"],
                "source_file": r["source_file"],
                "source_name": r["source_file"].rsplit("/", 1)[-1],
                "source_url": cms_url.get(r["source_file"]),
                "posted": r["cms_posted_date"],
            })
        sources = {}
        for h in history:
            sources.setdefault(h["source_file"], h)
        units = {" ".join((r["hcpcs_dosage"] or "").upper().split()) for r in cur_rows.values()}
        code_pages.append({
            "code": code,
            "description": display_text(newest["short_description"]),
            "other_descriptions": sorted({h["description"] for h in history} - {display_text(newest["short_description"])}),
            "dosage": newest["hcpcs_dosage"],
            "newest": newest,
            "in_latest": code in latest,
            "first_q": min(cur_rows),
            "last_q": max(cur_rows),
            "unit_changed": len(units) > 1,
            "chart_labels": [quarter_label(q) for q in quarters],
            # Chart points as text so no float rounding happens in Python;
            # null where the code is missing or CMS gave no payment limit.
            "chart_values": [plain_decimal(cur_rows[q]["payment_limit"]) or None
                             if q in cur_rows else None for q in quarters],
            "history": history,
            "sources": sorted(sources.values(), key=lambda h: h["quarter"], reverse=True),
        })

    # ---- Write everything --------------------------------------------------
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "codes").mkdir(parents=True)
    (OUT / "downloads").mkdir()
    shutil.copy2(STYLESHEET, OUT / "style.css")

    env = make_env()
    common = {
        "nav": NAV,
        "latest_q": latest_q,
        "source_page": manifest["source_page"],
        "top_n": TOP_N,
    }

    def write(path: str, template: str, **context) -> None:
        root = "../" * path.count("/")
        page = env.get_template(template).render(root=root, current_page=path, **common, **context)
        (OUT / path).write_text(page, encoding="utf-8")

    write("index.html", "index.html", s=summary)
    write("movers.html", "movers.html", s=summary, increases=increases, decreases=decreases)
    write("changes.html", "changes.html", s=summary, new_codes=new_codes, dropped_codes=dropped_codes)
    for p in code_pages:
        write(f"codes/{p['code']}.html", "code.html", p=p)
    write("search.html", "search.html", codes=code_pages)

    csv_path = OUT / "downloads" / CSV_NAME
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["effective_quarter", "hcpcs_code", "short_description", "hcpcs_dosage",
                    "payment_limit", "payment_limit_as_published", "source_file",
                    "source_url", "cms_posted_date", "file_version", "is_current"])
        for r in rows:
            w.writerow([r["effective_quarter"], r["hcpcs_code"], r["short_description"],
                        r["hcpcs_dosage"], plain_decimal(r["payment_limit"]),
                        r["payment_limit_text"], r["source_file"],
                        cms_url.get(r["source_file"], ""), r["cms_posted_date"] or "",
                        r["file_version"], str(r["is_current"]).lower()])
    write("downloads.html", "downloads.html", csv_name=CSV_NAME, csv_rows=len(rows),
          csv_kb=round(csv_path.stat().st_size / 1024), first_q=quarters[0],
          n_codes_all=len(code_pages), n_versions=len({(r["source_file"]) for r in rows}))

    write("about.html", "about.html", sales_q=quarter_offset(latest_q, -2),
          last_download=last_download.date(), built=date.today(), first_q=quarters[0])

    n_pages = sum(1 for _ in OUT.rglob("*.html"))
    print(f"Built {OUT.relative_to(ROOT)}/ : {n_pages:,} pages, latest quarter {latest_q}")
    print(f"  {summary['n_codes']:,} codes in {latest_q}; {summary['compared']:,} compared with {prior_q}")
    print(f"  up {summary['up']:,}, down {summary['down']:,}, unchanged {summary['same']:,}; "
          f"median change {percent(summary['median_pct'])}")
    print(f"  new {summary['n_new']:,}, dropped {summary['n_dropped']:,}")
    print(f"  left out of comparison: {summary['no_limit']} no limit, "
          f"{summary['unit_changed']} billing unit changed, {summary['zero_prior']} prior limit $0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
