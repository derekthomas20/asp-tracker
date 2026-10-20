"""Build the public website in public/ from data/asp.duckdb.

Every number on the site is read from the database by this script, and the
database is built from the CMS files in raw/ by scripts/parse.py. Nothing is
typed in by hand.

Pages written to public/:
    index.html          the latest quarter: lead mover, lists, summary paragraph
    movers.html         biggest increases and decreases vs the prior quarter
    changes.html        codes new or dropped this quarter
    coinsurance.html    codes with reduced (inflation-adjusted) coinsurance
    biosimilars.html    reference products and their biosimilars, grouped by
    biosimilars/X.html  the owner's sheet (verified rows only); prices from CMS;
                        Medicare spending, claims share and states from the
                        data.cms.gov usage files
    codes/XXXXX.html    one page per HCPCS code: chart, products (NDCs),
                        Medicare usage by year (data.cms.gov files),
                        table, CMS source links
    search.html         find a code by code or drug name
    downloads.html      the full history as one CSV (in downloads/)
    about.html          where the data comes from and what it means
    tools/ncr-calculator/index.html
                        biosimilar net cost recovery calculator: products from
                        the sheet, packages from the CMS crosswalk, limits from
                        CMS, default doses from data/ncr_markets.json
    search-index.js     the list behind the header search suggestions
    charts.js           draws the Chart.js line charts in the site's style

Templates are in site/templates; site/style.css, site/charts.js, site/ncr.js,
site/usmap.js
and site/static/ (logo, favicons) are copied as they are. The layout follows the
"Broadsheet" design in design/; colors and fonts are the Rxcurve brand.
public/ is deleted and rebuilt from scratch on every run.

Usage (from the repository root, after scripts/parse.py):
    .venv\\Scripts\\python scripts/build_site.py
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import html
import json
import re
import shutil
import statistics
import sys
from collections import Counter
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "asp.duckdb"
MANIFEST_PATH = ROOT / "data" / "manifest.json"
CMS_DATA_MANIFEST = ROOT / "data" / "cms_data_manifest.json"  # data.cms.gov usage files
TEMPLATES = ROOT / "site" / "templates"
STYLESHEET = ROOT / "site" / "style.css"
CHART_SCRIPT = ROOT / "site" / "charts.js"
NCR_SCRIPT = ROOT / "site" / "ncr.js"
MAP_SCRIPT = ROOT / "site" / "usmap.js"  # state map on the biosimilar group pages
NCR_MARKETS = ROOT / "data" / "ncr_markets.json"  # hand-maintained doses; holds no prices
NCR_PAGE = "tools/ncr-calculator/index.html"
STATIC = ROOT / "site" / "static"  # logo and favicons
OUT = ROOT / "public"
CSV_NAME = "asp-payment-limits-history.csv"

TOP_N = 25  # rows in each movers table
# Codes whose prior-quarter limit is below this are left out of the percent
# ranking: a few cents of change on a tiny base looks like a huge percent.
# They still appear in the dollar ranking.
PCT_MIN_PRIOR = Decimal("1")
SPARK_QUARTERS = 8  # quarters shown in the small trend lines and home chart
AROUND_LEAD = 6  # rows in the home page "Also rising" / "Largest decreases" lists
IRA_LIST = 8  # rows in the home page reduced coinsurance list
# Chart colors for biosimilars, in order (defined in site/style.css); the
# reference product is the thick navy line.
GROUP_SHADES = ["--color-teal", "--color-teal-light", "--color-series-3",
                "--color-series-4", "--color-series-5", "--color-series-6"]
REBATE_PAGE = ("https://www.cms.gov/initiatives/medicare-prescription-drug-affordability/"
               "overview/medicare-inflation-rebate-program")

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


def cents(value: Decimal | None) -> str:
    """A computed dollar amount (not a CMS figure) rounded to the cent."""
    if value is None:
        return "N/A"
    return money(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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


def arrow_percent(value: Decimal | None) -> str:
    """'↑ 4.4%' / '↓ 1.2%', the style used in the movers lists."""
    if value is None:
        return "N/A"
    arrow = "↑ " if value > 0 else "↓ " if value < 0 else ""
    return f"{arrow}{abs(value):.1f}%"


def plain_percent(value: str | None) -> str:
    """CMS coinsurance text such as '20.000' -> '20%', '11.501' -> '11.501%'.
    Only trailing zeros are dropped; nothing is rounded."""
    if not value:
        return "N/A"
    try:
        return f"{Decimal(value).normalize():f}%"
    except ArithmeticError:
        return value


def number(value: int) -> str:
    return f"{value:,}"


def big_money(value: Decimal | None) -> str:
    """Large dollar totals for headlines: '$1.23 billion', '$45.6 million',
    '$789,012'. Tables show the exact amount next to it."""
    if value is None:
        return "N/A"
    for size, word in ((Decimal("1e9"), "billion"), (Decimal("1e6"), "million")):
        if abs(value) >= size:
            return f"${value / size:,.{1 if abs(value) >= size * 10 else 2}f} {word}"
    return f"${value:,.0f}"


def short_money(value: Decimal | None) -> str:
    """Headline figures in a narrow column: '$439.3M', '$2.70B'."""
    return big_money(value).replace(" billion", "B").replace(" million", "M")


def share_text(value: Decimal | None) -> str:
    """A computed share, to one decimal place: '42.7%'."""
    return "N/A" if value is None else f"{value:.1f}%"


def count(value) -> str:
    """CMS counts (claims, units, services): commas, and every decimal CMS
    gave ('1,733,604', '13,002,343.6')."""
    if value is None:
        return "N/A"
    value = Decimal(value).normalize()
    places = max(0, -value.as_tuple().exponent)
    return f"{value:,.{places}f}"


def quarter_label(q: str) -> str:
    """'2026Q4' -> '4Q26', the short form the design uses."""
    return f"{q[5]}Q{q[2:4]}"


def quarter_months(q: str) -> str:
    first, last = QUARTER_MONTHS[q[5]]
    return f"{first}–{last} {q[:4]}"


def effective_range(q: str) -> str:
    """'2026Q4' -> 'October 1 – December 31, 2026'."""
    year, quarter = int(q[:4]), int(q[5])
    last_month = quarter * 3
    last_day = calendar.monthrange(year, last_month)[1]
    first, last = QUARTER_MONTHS[q[5]]
    return f"{first} 1 – {last} {last_day}, {year}"


def sparkline(values: list[Decimal | None], width: int = 110, height: int = 28) -> str:
    """A tiny SVG line of the given payment limits (None = gap), with a dot on
    the last point. Values are turned into float pixel positions for drawing
    only; the numbers shown in text elsewhere are the exact ones."""
    points = [float(v) for v in values if v is not None]
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    pad = (hi - lo or hi * 0.1 or 1) * 0.12
    lo, hi = max(0.0, lo - pad), hi + pad
    step = (width - 8) / (len(values) - 1)
    x = lambda i: 4 + i * step
    y = lambda v: 4 + (1 - (float(v) - lo) / (hi - lo)) * (height - 8)
    path, pen_down = [], False
    for i, v in enumerate(values):
        if v is None:
            pen_down = False
            continue
        path.append(f"{'L' if pen_down else 'M'}{x(i):.1f} {y(v):.1f}")
        pen_down = True
    dot = ""
    if values[-1] is not None:
        dot = f'<circle cx="{x(len(values) - 1):.1f}" cy="{y(values[-1]):.1f}" r="3"/>'
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'aria-hidden="true"><path d="{" ".join(path)}"/>{dot}</svg>')


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
               payment_limit, payment_limit_text, coinsurance_percentage,
               vaccine_awp_pct, blood_awp_pct, clotting_factor, notes,
               source_file, cms_posted_date, file_version, is_current
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


# Phrases in the CMS "Notes" column that say how a payment limit was set.
# Only what CMS writes is shown; a code with none of these (and none of the
# vaccine, blood or clotting factor columns filled in) gets "Not stated".
BASIS_NOTES = [
    ("8% of reference add-on applied", "Biosimilar: 8% of reference product add-on applied"),
    ('"lesser of" methodology applied', '"Lesser of" methodology applied'),
    ("AMP-based payment limit", "AMP-based payment limit"),
]
INFLATION_NOTE = "Inflation-adjusted coinsurance"


def to_decimal(text: str | None) -> Decimal | None:
    try:
        return Decimal(text) if text and text.strip() else None
    except ArithmeticError:
        return None


def payment_basis(r: dict) -> list[str]:
    """How CMS says this payment limit was set, from the file's own columns."""
    out = []
    if r["vaccine_awp_pct"]:
        out.append(f"Vaccine: {plain_percent(r['vaccine_awp_pct'])} of AWP")
    if r["blood_awp_pct"]:
        out.append(f"Blood product: {plain_percent(r['blood_awp_pct'])} of AWP")
    if r["clotting_factor"]:
        out.append("Clotting factor")
    notes = (r["notes"] or "").lower()
    out += [label for phrase, label in BASIS_NOTES if phrase.lower() in notes]
    return out


def load_groups(con) -> list[dict]:
    """Rows of the owner's biosimilar groups sheet (see scripts/fetch_groups.py)."""
    cur = con.execute("SELECT * FROM biosimilar_groups ORDER BY source_row")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def build_groups(group_rows: list[dict], latest: dict[str, dict], recent_limits, warn) -> list[dict]:
    """Turn verified sheet rows into one comparison per molecule, using CMS
    payment limits for every price. Anything that can't be compared is left
    out and reported through warn()."""
    verified = [g for g in group_rows if g["verified"]]
    waiting = [g for g in group_rows if not g["verified"]]
    if waiting:
        per_molecule = Counter(g["molecule"] for g in waiting)
        warn(f"{len(waiting)} sheet rows are not verified and are not published: "
             + ", ".join(f"{m} ({n})" for m, n in sorted(per_molecule.items())))

    by_molecule: dict[str, list[dict]] = {}
    for g in verified:
        r = latest.get(g["hcpcs_code"])
        if r is None:
            warn(f"{g['hcpcs_code']} ({g['molecule']}) is verified but not in the latest CMS file; left out.")
            continue
        if g["cms_description"] and g["cms_description"] != r["short_description"]:
            warn(f"{g['hcpcs_code']}: sheet description {g['cms_description']!r} differs from "
                 f"CMS {r['short_description']!r}.")
        by_molecule.setdefault(g["molecule"], []).append({**g, "row": r})

    groups = []
    for molecule, members in sorted(by_molecule.items()):
        refs = [m for m in members if m["role"] == "reference"]
        bios = [m for m in members if m["role"] == "biosimilar"]
        if len(refs) != 1 or not bios:
            warn(f"{molecule}: needs exactly one verified reference and at least one verified "
                 f"biosimilar (has {len(refs)} and {len(bios)}); not published.")
            continue
        ref = refs[0]
        ref_limit, ref_unit = ref["row"]["payment_limit"], ref["row"]["hcpcs_dosage"]

        def entry(m: dict) -> dict:
            r = m["row"]
            limit = r["payment_limit"]
            vs_ref = None
            if (m is not ref and limit is not None and ref_limit
                    and same_unit(r["hcpcs_dosage"], ref_unit)):
                vs_ref = (limit - ref_limit) / ref_limit * 100
            elif m is not ref and not same_unit(r["hcpcs_dosage"], ref_unit):
                warn(f"{m['hcpcs_code']}: billing unit {r['hcpcs_dosage']!r} differs from reference "
                     f"{ref['hcpcs_code']} {ref_unit!r}; no comparison shown.")
            notes = (r["notes"] or "").lower()
            has_8pct_note = "8% of reference add-on applied" in notes
            if m is not ref and m["qualifies_8pct"].lower() in ("yes", "no"):
                if (m["qualifies_8pct"].lower() == "yes") != has_8pct_note:
                    warn(f"{m['hcpcs_code']}: sheet qualifies_8pct={m['qualifies_8pct']!r} but the CMS "
                         f"note {'has' if has_8pct_note else 'lacks'} the 8% add-on.")
            spark_values = recent_limits(m["hcpcs_code"])
            return {
                "code": m["hcpcs_code"],
                "name": (m["brand_names"].split(";")[0].strip() or display_text(r["short_description"])),
                "brands": m["brand_names"],
                "description": display_text(r["short_description"]),
                "role": m["role"],
                "limit": limit,
                "dosage": r["hcpcs_dosage"],
                "vs_ref": vs_ref,
                "cms_8pct": has_8pct_note,
                "chart_values": [plain_decimal(v) or None for v in spark_values],
                "spark": Markup(sparkline(spark_values)),
            }

        ref_entry = entry(ref)
        bio_entries = sorted((entry(b) for b in bios),
                             key=lambda e: (e["limit"] is None, e["limit"] or 0, e["code"]))
        for i, e in enumerate(bio_entries):
            e["color_var"] = GROUP_SHADES[i % len(GROUP_SHADES)]
        ref_entry["color_var"] = "--color-navy"
        compared = [e for e in bio_entries if e["vs_ref"] is not None]
        lowest = min(compared, key=lambda e: e["vs_ref"]) if compared else None
        groups.append({
            "molecule": molecule,
            "slug": slugify(molecule),
            "ref": ref_entry,
            "bios": bio_entries,
            "lowest": lowest,
            "avg_vs_ref": (sum(e["vs_ref"] for e in compared) / len(compared)) if compared else None,
            "n_compared": len(compared),
            "sources": sorted({m["source_file"] for m in members}),
        })
    return groups


# --------------------------------------------------------------------------
# Medicare usage (data.cms.gov files; see scripts/cms_data.py)
# --------------------------------------------------------------------------

def load_usage(con) -> tuple[dict[str, dict[int, dict]], dict[int, dict], dict | None]:
    """Medicare Part B spending per code per calendar year, from the best
    source CMS has for each year:

      1. the yearly Part B Spending by Drug file (final; has dosage units);
      2. otherwise the full-year ("Q1-Q4") rows of the quarterly file,
         marked preliminary. That file has no dosage units.

    A year uses one source for every code, so codes are never compared
    across sources within a year. When CMS posts the yearly file for a year
    that so far came from the quarterly file, the yearly file takes over.

    Returns (usage, years, ytd):
      usage  {code: {year: {spending, claims, beneficiaries, units, per_unit, source_file}}}
      years  {year: {"preliminary": bool, "source_file": set of files}}
      ytd    the newest partial year from the newest quarterly release
             ({label, year, quarters, by_code: {code: {spending, claims}}, source_files}),
             or None
    """
    usage: dict[str, dict[int, dict]] = {}
    years: dict[int, dict] = {}
    for code, year, spend, units, claims, benes, per_unit, src in con.execute("""
            SELECT hcpcs_code, data_year, total_spending, total_dosage_units, total_claims,
                   total_beneficiaries, avg_spending_per_dosage_unit, source_file
            FROM part_b_spending WHERE is_latest_release""").fetchall():
        usage.setdefault(code, {})[year] = {"spending": spend, "claims": claims, "beneficiaries": benes,
                                            "units": units, "per_unit": per_unit, "source_file": src}
        years.setdefault(year, {"preliminary": False, "source_files": set()})["source_files"].add(src)

    quarterly = con.execute("""
        SELECT hcpcs_code, period_year, period_quarters, period_label, total_spending, total_claims,
               total_beneficiaries, source_file, release_period
        FROM part_b_spending_quarterly WHERE is_latest_release""").fetchall()
    final_years = set(years)
    for code, year, quarters, _, spend, claims, benes, src, _ in quarterly:
        if quarters != "Q1-Q4" or year in final_years:
            continue
        usage.setdefault(code, {})[year] = {"spending": spend, "claims": claims, "beneficiaries": benes,
                                            "units": None, "per_unit": None, "source_file": src}
        years.setdefault(year, {"preliminary": True, "source_files": set()})["source_files"].add(src)

    ytd = None
    partial = [r for r in quarterly if r[2] != "Q1-Q4" and r[1] not in years]
    if partial:
        newest = max(r[8] for r in partial)
        partial = [r for r in partial if r[8] == newest]
        year = max(r[1] for r in partial)
        partial = [r for r in partial if r[1] == year]
        labels = {r[3] for r in partial}
        if len(labels) != 1:
            raise SystemExit(f"STOPPED: quarterly release {newest} has several partial periods for {year}: {labels}")
        ytd = {"label": labels.pop(), "year": year, "quarters": partial[0][2],
               "by_code": {r[0]: {"spending": r[4], "claims": r[5], "beneficiaries": r[6]} for r in partial},
               "source_files": {r[7] for r in partial}}
    return usage, years, ytd


def load_geo(con, codes: set[str]) -> tuple[int | None, dict[str, dict[str, Decimal]], dict[str, str], set[str]]:
    """Physician-billed services per code per state, newest year of the
    Geography and Service file, summed over place of service.
    Returns (year, {code: {state code: services}}, {state code: name}, source files)."""
    year = con.execute("SELECT max(data_year) FROM physician_geo_service WHERE is_latest_release").fetchone()[0]
    if year is None or not codes:
        return None, {}, {}, set()
    placeholders = ", ".join("?" for _ in codes)
    rows = con.execute(f"""
        SELECT hcpcs_code, geo_level, geo_code, geo_description, sum(total_services), any_value(source_file)
        FROM physician_geo_service
        WHERE is_latest_release AND data_year = ? AND hcpcs_code IN ({placeholders})
        GROUP BY ALL""", [year, *sorted(codes)]).fetchall()
    services: dict[str, dict[str, Decimal]] = {}
    names, files = {}, set()
    for code, level, geo_code, desc, total, src in rows:
        key = "US" if level == "National" else geo_code
        if not key:
            continue
        services.setdefault(code, {})[key] = total
        if key != "US":
            names[key] = desc
        files.add(src)
    return year, services, names, files


def load_discarded(con) -> dict[str, dict]:
    """Newest year of the Discarded Drug Units file, per code:
    {code: {year, allowed, discarded, pct_discarded, source_file}}."""
    rows = con.execute("""
        SELECT hcpcs_code, data_year, total_allowed_amount, total_allowed_discarded_amount,
               pct_discarded_units, source_file
        FROM discarded_drug_units
        WHERE is_latest_release AND data_year = (SELECT max(data_year) FROM discarded_drug_units)
    """).fetchall()
    return {code: {"year": year, "allowed": allowed, "discarded": disc, "pct_discarded": pct,
                   "source_file": src} for code, year, allowed, disc, pct, src in rows
            if pct is not None and disc is not None and allowed is not None}  # some CMS rows are blank


TOP_STATES = 10  # rows in the code page's top states list


def build_code_usage(code: str, usage, years_info, ytd, geo, discarded) -> dict | None:
    """Medicare usage for one code page: one row per year (final years from
    the yearly file, then preliminary full years from the quarterly file),
    spending so far this year, the discarded-drug share, and the states
    with the most physician-billed services."""
    by_year = usage.get(code, {})
    years = []
    for y in sorted(by_year):
        r = by_year[y]
        years.append({"year": y, "preliminary": years_info[y]["preliminary"], **r})
    ytd_row = ytd["by_code"].get(code) if ytd else None
    disc = discarded.get(code)
    geo_year, geo_services, state_names, geo_files = geo
    services = geo_services.get(code, {})
    states = sorted(((state_names.get(k, k), v) for k, v in services.items() if k != "US"),
                    key=lambda s: (-s[1], s[0]))
    if not years and not ytd_row and not disc and not states:
        return None
    national = services.get("US")
    coverage = None
    if national is not None and geo_year in by_year and not years_info[geo_year]["preliminary"]:
        coverage = share(national, by_year[geo_year]["units"])
    files = {y["source_file"] for y in years}
    if ytd_row:
        files |= ytd["source_files"]
    if disc:
        files.add(disc["source_file"])
    if states:
        files |= {f for f in geo_files if f"/{geo_year}/" in f}
    latest = years[-1] if years else None
    return {
        "years": list(reversed(years)),  # newest first in the table
        "latest": latest,
        "has_units": any(y["units"] is not None for y in years),
        "ytd": ({**ytd_row, "label": ytd["label"], "year": ytd["year"], "quarters": ytd["quarters"]}
                if ytd_row else None),
        "discarded": disc,
        "geo_year": geo_year if states else None,
        "states": [{"name": n, "services": v, "share": share(v, national)} for n, v in states[:TOP_STATES]],
        "n_states": len(states),
        "geo_coverage": coverage,
        "chart_labels": [[str(y["year"]), "prelim."] if y["preliminary"] else str(y["year"]) for y in years],
        "chart_series": [{"label": "Medicare spending", "colorVar": "--color-teal",
                          "values": [plain_decimal(y["spending"]) or None for y in years]}],
        "source_files": sorted(files),
    }


def share(part, whole) -> Decimal | None:
    """part / whole as a percent, or None when whole is zero or unknown."""
    if part is None or not whole:
        return None
    return Decimal(part) / Decimal(whole) * 100


def build_market(grp: dict, usage, years_info, ytd, geo, units_in_year) -> dict | None:
    """Medicare usage for one biosimilar group: spending and claims by year,
    biosimilar share of claims, the latest year in detail, and physician-billed
    services by state. Every figure is a CMS figure or a sum/share of them."""
    members = [grp["ref"], *grp["bios"]]
    codes = [e["code"] for e in members]
    bio_codes = set(codes[1:])
    seen = sorted({y for c in codes for y in usage.get(c, {})})
    if not seen:
        return None

    per_year = []
    for y in range(seen[0], seen[-1] + 1):
        info = years_info.get(y)
        if info is None:
            continue
        prod = {c: usage.get(c, {}).get(y) for c in codes}
        listed = [p for p in prod.values() if p]
        claims_known = all(p["claims"] is not None for p in listed)
        total_claims = sum(p["claims"] for p in listed if p["claims"] is not None)
        bio_claims = sum(prod[c]["claims"] for c in bio_codes if prod[c] and prod[c]["claims"] is not None)
        per_year.append({
            "year": y,
            "label": f"{y} (prelim.)" if info["preliminary"] else str(y),
            "preliminary": info["preliminary"],
            "spending": sum(p["spending"] for p in listed if p["spending"] is not None),
            "claims": total_claims if claims_known else None,
            "bio_share": share(bio_claims, total_claims) if claims_known else None,
            "by_code": prod,
            "claim_share": {c: (share(prod[c]["claims"], total_claims) if claims_known and prod[c] else None)
                            for c in codes},
        })
    if not per_year:
        return None
    latest = per_year[-1]
    final = next((y for y in reversed(per_year) if not y["preliminary"]), None)
    prior = per_year[-2] if len(per_year) > 1 else None

    products = []
    for e in members:
        f = final["by_code"].get(e["code"]) if final else None
        l = latest["by_code"].get(e["code"])
        products.append({
            **{k: e[k] for k in ("code", "name", "role", "color_var")},
            "final": f, "latest": l,
            "latest_share": latest["claim_share"].get(e["code"]),
            "ytd": (ytd["by_code"].get(e["code"]) if ytd else None),
        })

    # Charts: values as text so no float rounding happens here.
    def series(value):
        return [{"label": e["name"], "colorVar": e["color_var"],
                 "values": [value(y, e["code"]) for y in per_year]} for e in members]
    spend_series = series(lambda y, c: plain_decimal(y["by_code"][c]["spending"]) if y["by_code"][c] else None)
    # Market share lines: each product's share of the group's claims, the
    # reference as the thick navy line, and all biosimilars together dashed.
    share_series = [{"label": e["name"], "colorVar": e["color_var"],
                     "width": 3.5 if e["role"] == "reference" else 1.75,
                     "values": [f"{y['claim_share'][e['code']]:.2f}" if y["claim_share"][e["code"]] is not None
                                else None for y in per_year]} for e in members]
    if len(bio_codes) > 1:
        share_series.append({"label": "All biosimilars", "colorVar": "--color-highlight", "width": 2.5,
                             "dash": True, "values": [f"{y['bio_share']:.2f}" if y["bio_share"] is not None
                                                      else None for y in per_year]})

    # Volume: claims for the latest year; billing units for the latest final
    # year, only when every code with units that year is billed in the same unit.
    final_units = final_unit = None
    if final:
        with_units = [c for c in codes if final["by_code"].get(c) and final["by_code"][c]["units"] is not None]
        unit_sets = [units_in_year.get((c, final["year"]), set()) for c in with_units]
        if with_units and all(len(u) == 1 and u == unit_sets[0] for u in unit_sets):
            final_units = sum(final["by_code"][c]["units"] for c in with_units)
            final_unit = next(iter(unit_sets[0]))

    # States: physician-billed services, only for codes billed in the same
    # unit as the reference product in that year (services count billing units).
    geo_year, geo_services, state_names, geo_files = geo
    states, excluded, coverage = [], [], None
    if geo_year is not None:
        ref_units = units_in_year.get((codes[0], geo_year), set())
        comparable = [c for c in codes if len(ref_units) == 1 and units_in_year.get((c, geo_year)) == ref_units]
        excluded = [e["name"] for e in members if e["code"] not in comparable and geo_services.get(e["code"])]
        if comparable and any(geo_services.get(c) for c in comparable):
            for key in sorted({k for c in comparable for k in geo_services.get(c, {})} - {"US"}):
                total = sum(geo_services.get(c, {}).get(key, 0) for c in comparable)
                bios = sum(geo_services.get(c, {}).get(key, 0) for c in comparable if c in bio_codes)
                if total:
                    states.append({"code": key, "name": state_names.get(key, key), "services": total,
                                   "bio_share": share(bios, total)})
            states.sort(key=lambda s: (-s["services"], s["name"]))
            # Share of the year's Medicare units that the physician-billed
            # file covers, when the yearly spending file has that year.
            nat = sum(geo_services.get(c, {}).get("US", 0) for c in comparable)
            units = [usage.get(c, {}).get(geo_year, {}).get("units") for c in comparable
                     if geo_services.get(c, {}).get("US")]
            if units and all(u is not None for u in units) and not years_info.get(geo_year, {}).get("preliminary", True):
                coverage = share(nat, sum(units))
    return {
        "years": per_year, "latest": latest, "final": final, "prior": prior,
        "products": products, "ytd": ytd,
        "ytd_spending": (sum(p["ytd"]["spending"] for p in products if p["ytd"])
                         if any(p["ytd"] for p in products) else None),
        # A preliminary year gets a second label line ("2025" / "prelim.").
        "chart_labels": [[str(y["year"]), "prelim."] if y["preliminary"] else str(y["year"]) for y in per_year],
        "spend_series": spend_series, "share_series": share_series,
        "final_units": final_units, "final_unit": final_unit,
        "geo_year": geo_year, "states": states, "geo_excluded": excluded, "geo_coverage": coverage,
        # For the map: FIPS state code -> [services, biosimilar share], as text.
        "map_data": {s["code"]: [plain_decimal(s["services"]),
                                 f"{s['bio_share']:.2f}" if s["bio_share"] is not None else None,
                                 s["name"]] for s in states},
        "source_files": sorted(set().union(*(years_info[y["year"]]["source_files"] for y in per_year),
                                           ytd["source_files"] if ytd else set(),
                                           geo_files if states else set())),
    }


def load_products(con, quarter: str) -> dict[str, list[dict]]:
    """NDC products mapped to each HCPCS code in the quarter's current
    NDC-HCPCS crosswalk files. One entry per NDC (an NDC listed in more than
    one crosswalk is shown once)."""
    cur = con.execute("""
        SELECT hcpcs_code, crosswalk_type, drug_name, labeler_name, ndc,
               bill_units_pkg, source_file, cms_posted_date
        FROM crosswalks
        WHERE is_current AND effective_quarter = ? AND hcpcs_code IS NOT NULL
        ORDER BY hcpcs_code, drug_name, ndc, crosswalk_type
    """, [quarter])
    names = [d[0] for d in cur.description]
    out: dict[str, list[dict]] = {}
    seen = set()
    for r in (dict(zip(names, row)) for row in cur.fetchall()):
        if (r["hcpcs_code"], r["ndc"]) in seen:
            continue
        seen.add((r["hcpcs_code"], r["ndc"]))
        try:
            r["units"] = Decimal((r["bill_units_pkg"] or "").replace(",", ""))
        except ArithmeticError:
            r["units"] = None  # not a number in the CMS file; shown as given
        out.setdefault(r["hcpcs_code"], []).append(r)
    return out


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
# NCR calculator: products from the biosimilar groups sheet, packages from
# the CMS crosswalk, limits from the CMS payment limit file, default doses
# from data/ncr_markets.json
# --------------------------------------------------------------------------

DOSE_BASIS = {"per kg": "mgkg", "per m2": "mgm2", "flat": "flat"}  # names used in site/ncr.js
UNIT_NAMES = {"MG": "mg", "MCG": "mcg", "UNIT": "units", "UNITS": "units"}


def billing_unit(text: str | None) -> tuple[Decimal, str] | None:
    """(size, measure) of one billing unit from CMS dosage text such as
    '10 MG', '0.5 MG', '1 MCG' or '1000 UNITS'. None if not in that form."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(MG|MCG|UNITS?)\s*", text or "", re.IGNORECASE)
    return (Decimal(m.group(1)), UNIT_NAMES[m.group(2).upper()]) if m else None


def load_packages(con, quarter: str) -> dict[str, set[tuple[str, Decimal, str]]]:
    """Billing units in one package (one vial or syringe) for each HCPCS code,
    from the quarter's ASP NDC-HCPCS crosswalk:
    {code: {(drug name, billing units, CMS package size text)}}.
    CMS gives billing units per NDC, which may be a carton of several
    packages, so they are divided by the package quantity."""
    rows = con.execute("""
        SELECT DISTINCT hcpcs_code, drug_name, bill_units_pkg, pkg_qty, pkg_size
        FROM crosswalks
        WHERE effective_quarter = ? AND is_current AND crosswalk_type = 'ASP'
    """, [quarter]).fetchall()
    out: dict[str, set] = {}
    for code, name, units_pkg, qty, size in rows:
        units, n = to_decimal(units_pkg), to_decimal(qty)
        if not name or units is None or not n:
            raise SystemExit(f"STOPPED: {quarter} crosswalk row for {code} has drug name {name!r}, "
                             f"billing units {units_pkg!r}, package quantity {qty!r}.")
        out.setdefault(code, set()).add((name.strip(), units / n, (size or "").strip()))
    return out


def number_text(value: Decimal) -> str:
    return f"{value.normalize():f}"


def build_ncr(groups: list[dict], packages: dict[str, set], warn) -> list[dict]:
    """One calculator market per entry in data/ncr_markets.json. Products are
    the published biosimilar group for the market's molecule (one product per
    HCPCS code and crosswalk drug name), with CMS payment limits and package
    sizes. Things that would make the math wrong stop the build."""
    with NCR_MARKETS.open(encoding="utf-8") as f:
        config = json.load(f)
    group_of = {g["molecule"]: g for g in groups}
    covered = set()
    markets = []
    for mk in config["markets"]:
        where = f"{NCR_MARKETS.relative_to(ROOT)}, market {mk['id']}"
        if mk["dose"]["basis"] not in DOSE_BASIS:
            raise SystemExit(f"STOPPED: {where}: dose basis {mk['dose']['basis']!r} "
                             f"is not one of {list(DOSE_BASIS)}.")
        g = group_of.get(mk["molecule"])
        if g is None:
            warn(f"NCR calculator: {mk['id']}: no published biosimilar group for {mk['molecule']!r}; "
                 "market left out.")
            continue
        covered.add(mk["molecule"])
        members = [g["ref"], *g["bios"]]

        # The calculator rounds a dose to whole billing units of one size per
        # market, so every code must use the same unit.
        units_seen: dict[tuple, list[str]] = {}
        for e in members:
            bu = billing_unit(e["dosage"])
            if bu is None:
                raise SystemExit(f"STOPPED: {where}: CMS dosage {e['dosage']!r} for {e['code']} is not "
                                 "in the form '<number> MG', 'MCG' or 'UNITS'.")
            units_seen.setdefault(bu, []).append(e["code"])
        if len(units_seen) != 1:
            found = "; ".join(f"{s} {m}: {', '.join(c)}" for (s, m), c in units_seen.items())
            raise SystemExit(f"STOPPED: {where}: products need one billing unit per market ({found}).")
        ((size, measure),) = units_seen

        only = {Decimal(str(u)) for u in mk.get("only_units", [])}

        def multi_dose(units: Decimal, pkg_size: str) -> bool:
            return any(Decimal(str(sel["units"])) == units and sel.get("pkg_size", pkg_size) == pkg_size
                       for sel in mk.get("multi_dose", []))

        examples = mk.get("example_prices", {})
        used_examples = set()
        products = []
        for e in members:
            items = packages.get(e["code"], set())
            if not items:
                warn(f"NCR calculator: {e['code']} ({e['name']}) has no packages in the CMS crosswalk; "
                     "left out.")
                continue
            for name in sorted({n for n, _, _ in items}):
                pkgs = {}
                for n, units, pkg_size in items:
                    if n != name or (only and units not in only):
                        continue
                    if units != units.to_integral_value():
                        warn(f"NCR calculator: {name} ({e['code']}) has a package of {number_text(units)} "
                             "billing units, not a whole number.")
                    vial = "MDV" if multi_dose(units, pkg_size) else "SDV"
                    amount = number_text(units * size)
                    price = examples.get(name, {}).get(amount)
                    if price is not None:
                        used_examples.add((name, amount))
                    pkgs[(units, vial)] = {"key": f"{number_text(units)}-{vial}", "amount": float(units * size),
                                           "amount_text": amount, "units": float(units), "type": vial,
                                           "example_price": price}
                if not pkgs:
                    continue
                generic = mk["molecule"].lower()
                unbranded = name.lower() == generic or name.lower().startswith(generic + "-")
                products.append({
                    "key": slugify(f"{name} {e['code']}"),
                    "name": f"{name} (unbranded)" if unbranded else name,
                    "example_name": name,
                    "hcpcs": e["code"],
                    "reference": e["role"] == "reference",
                    "status": "cms" if e["limit"] is not None else "no_limit",
                    "cms_limit": plain_decimal(e["limit"]) if e["limit"] is not None else None,
                    "cms_dosage": e["dosage"],
                    "example_rebate_pct": mk.get("example_rebates", {}).get(name, 0),
                    "packages": [pkgs[k] for k in sorted(pkgs)],
                })
                if e["limit"] is None:
                    warn(f"NCR calculator: {e['code']} ({name}) has no payment limit in the latest CMS file.")
        unused = [f"{n} {a}" for n, ps in examples.items() for a in ps if (n, a) not in used_examples]
        if unused:
            warn(f"NCR calculator: {mk['id']}: example prices match no package: {', '.join(unused)}.")
        products.sort(key=lambda p: (not p["reference"], p["name"].lower()))
        for p in products:
            del p["example_name"]
        markets.append({"id": mk["id"], "name": mk["name"], "unit_size": float(size), "unit_name": measure,
                        "unit_label": f"{size.normalize():,f} {measure}",
                        "dose_source": mk["dose_source"],
                        "dose": {"basis": DOSE_BASIS[mk["dose"]["basis"]], "value": mk["dose"]["value"],
                                 "weight": mk["dose"]["weight_kg"], "bsa": mk["dose"]["bsa_m2"]},
                        "products": products})
    for g in groups:
        if g["molecule"] not in covered:
            warn(f"NCR calculator: biosimilar group {g['molecule']!r} has no market in "
                 f"{NCR_MARKETS.relative_to(ROOT)}.")
    return markets


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
    env.filters.update(money=money, cents=cents, signed_money=signed_money, percent=percent,
                       arrow_percent=arrow_percent, plain_percent=plain_percent,
                       num=number, quarter=quarter_label, months=quarter_months,
                       effective=effective_range, long_date=long_date, desc=display_text,
                       big_money=big_money, short_money=short_money, share=share_text, count=count)
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
    quarters = sorted({r["effective_quarter"] for r in rows})
    if len(quarters) < 2:
        raise SystemExit("STOPPED: need at least two quarters of data to compare.")
    latest_q, prior_q = quarters[-1], quarters[-2]
    products = load_products(con, latest_q)
    ncr_packages = load_packages(con, latest_q)
    group_rows = load_groups(con)
    usage, usage_years, usage_ytd = load_usage(con)
    geo = load_geo(con, {r["hcpcs_code"] for r in rows})
    discarded = load_discarded(con)
    con.close()
    if CMS_DATA_MANIFEST.exists():
        with CMS_DATA_MANIFEST.open(encoding="utf-8") as f:
            for e in json.load(f)["files"]:
                cms_url[e["local_path"]] = e["url"]
    warnings: list[str] = []

    current = [r for r in rows if r["is_current"]]
    by_quarter: dict[str, dict[str, dict]] = {}
    for r in current:
        by_quarter.setdefault(r["effective_quarter"], {})[r["hcpcs_code"]] = r
    latest, prior = by_quarter[latest_q], by_quarter[prior_q]
    recent_qs = quarters[-SPARK_QUARTERS:]

    def recent_limits(code: str) -> list[Decimal | None]:
        """Payment limits for the last few quarters, for sparklines. A quarter
        whose billing unit differs from the latest one is left as a gap, so
        the line never joins prices for different units."""
        unit = (latest.get(code) or prior.get(code) or {}).get("hcpcs_dosage")
        out = []
        for q in recent_qs:
            r = by_quarter[q].get(code)
            out.append(r["payment_limit"] if r and same_unit(r["hcpcs_dosage"], unit) else None)
        return out

    posted = {r["cms_posted_date"] for r in latest.values()}
    if len(posted) != 1:
        raise SystemExit(f"STOPPED: {latest_q} current rows have {len(posted)} posted dates.")

    # ---- Comparison with the prior quarter --------------------------------
    cmp = compare(latest, prior)
    compared = cmp["compared"]
    ups = [c for c in compared if c["change"] > 0]
    downs = [c for c in compared if c["change"] < 0]
    pct_ok = lambda c: c["prior"] >= PCT_MIN_PRIOR
    movers = {
        "pct": {
            "increases": sorted(filter(pct_ok, ups), key=lambda c: -c["pct"])[:TOP_N],
            "decreases": sorted(filter(pct_ok, downs), key=lambda c: c["pct"])[:TOP_N],
        },
        "dollars": {
            "increases": sorted(ups, key=lambda c: -c["change"])[:TOP_N],
            "decreases": sorted(downs, key=lambda c: c["change"])[:TOP_N],
        },
    }
    n_below_floor = sum(1 for c in compared if c["change"] != 0 and not pct_ok(c))
    for c in compared:
        c["spark"] = Markup(sparkline(recent_limits(c["code"])))

    def code_row(r: dict) -> dict:
        return {"code": r["hcpcs_code"], "description": display_text(r["short_description"]),
                "dosage": r["hcpcs_dosage"], "limit": r["payment_limit"]}

    # ---- Reduced coinsurance ----------------------------------------------
    # "Usual" coinsurance is the percentage most codes in this quarter's file
    # carry (computed, not typed in). Reduced = above 0% and below that.
    co_values = [to_decimal(r["coinsurance_percentage"]) for r in latest.values()]
    co_counts = Counter(v for v in co_values if v is not None)
    if not co_counts:
        raise SystemExit(f"STOPPED: no coinsurance percentages in {latest_q}.")
    usual_co, usual_co_n = co_counts.most_common(1)[0]
    reduced = []
    for r in latest.values():
        co = to_decimal(r["coinsurance_percentage"])
        if co is None or not (0 < co < usual_co):
            continue
        saves = None
        if r["payment_limit"] is not None:
            saves = r["payment_limit"] * (usual_co - co) / 100
        reduced.append({**code_row(r), "co": co, "saves": saves})
    reduced.sort(key=lambda x: (x["co"], x["code"]))
    # CMS also writes "Inflation-adjusted coinsurance" in the notes. If the
    # note and the percentage ever disagree, say so in the build output.
    noted = {c for c, r in latest.items() if INFLATION_NOTE.lower() in (r["notes"] or "").lower()}
    mismatch = sorted(noted ^ {x["code"] for x in reduced})
    coinsurance = {"usual": usual_co, "usual_n": usual_co_n, "rows": reduced, "mismatch": mismatch,
                   "n_codes": len(latest), "top": reduced[:IRA_LIST]}
    reduced_by_code = {x["code"]: x for x in reduced}

    # ---- Biosimilar groups (owner's sheet; prices from CMS) ----------------
    groups = build_groups(group_rows, latest, recent_limits, warnings.append)
    # Billing units each code had in each calendar year, from the payment
    # limit files (usage counts are in billing units).
    units_in_year: dict[tuple[str, int], set[str]] = {}
    for r in current:
        units_in_year.setdefault((r["hcpcs_code"], int(r["effective_quarter"][:4])), set()).add(
            " ".join((r["hcpcs_dosage"] or "").upper().split()))
    for grp in groups:
        grp["market"] = build_market(grp, usage, usage_years, usage_ytd, geo, units_in_year)
        if grp["market"]:
            grp["market"]["sources"] = [{"name": f.rsplit("/", 1)[-1], "url": cms_url.get(f)}
                                        for f in grp["market"]["source_files"]]
    ncr_markets = build_ncr(groups, ncr_packages, warnings.append)
    group_of = {}
    for grp in groups:
        for e in [grp["ref"], *grp["bios"]]:
            group_of[e["code"]] = grp
    groups_source = sorted({r["source_file"] for r in group_rows})

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

        def change_since(quarters_back: int) -> Decimal | None:
            """Percent change from the quarter N back to the newest one, only
            when both have a payment limit and the same billing unit."""
            then = cur_rows.get(quarter_offset(newest["effective_quarter"], -quarters_back))
            if (then is None or then["payment_limit"] in (None, 0) or newest["payment_limit"] is None
                    or not same_unit(then["hcpcs_dosage"], newest["hcpcs_dosage"])):
                return None
            return (newest["payment_limit"] - then["payment_limit"]) / then["payment_limit"] * 100

        code_products = []
        for prod in products.get(code, []) if code in latest else []:
            per_pkg = None
            if prod["units"] is not None and newest["payment_limit"] is not None:
                per_pkg = prod["units"] * newest["payment_limit"]
            code_products.append({**prod, "per_package": per_pkg})
            sources.setdefault(prod["source_file"], {
                "quarter": latest_q, "is_current": True, "posted": prod["cms_posted_date"],
                "source_name": prod["source_file"].rsplit("/", 1)[-1],
                "source_url": cms_url.get(prod["source_file"]),
            })
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
            "qoq": change_since(1),
            "yoy": change_since(4),
            "coinsurance": newest["coinsurance_percentage"],
            "reduced": reduced_by_code.get(code),
            "basis": payment_basis(newest),
            "notes": display_text(newest["notes"]),
            "group": group_of.get(code),
            "products": code_products,
            "usage": build_code_usage(code, usage, usage_years, usage_ytd, geo, discarded),
        })
        u = code_pages[-1]["usage"]
        if u:
            u["sources"] = [{"name": f.rsplit("/", 1)[-1], "url": cms_url.get(f)} for f in u["source_files"]]

    # ---- Home page lead story ---------------------------------------------
    # The largest percent increase (same ranking and $1 floor as Movers), with
    # its real payment limits for the last few quarters.
    lead = None
    if movers["pct"]["increases"]:
        top = movers["pct"]["increases"][0]
        lead = {**top,
                "chart_labels": [quarter_label(q) for q in recent_qs],
                "chart_values": [plain_decimal(v) or None for v in recent_limits(top["code"])]}
    around = {
        "rising": movers["pct"]["increases"][1:1 + AROUND_LEAD],
        "falling": movers["pct"]["decreases"][:AROUND_LEAD],
    }

    # ---- Write everything --------------------------------------------------
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "codes").mkdir(parents=True)
    (OUT / "downloads").mkdir()
    shutil.copy2(STYLESHEET, OUT / "style.css")
    shutil.copy2(CHART_SCRIPT, OUT / "charts.js")
    shutil.copy2(NCR_SCRIPT, OUT / "ncr.js")
    shutil.copy2(MAP_SCRIPT, OUT / "usmap.js")
    shutil.copytree(STATIC, OUT / "static")

    # The header search box suggests codes as you type. Its list is written as
    # a script (not JSON) so it also works when a page is opened from disk.
    index = [[p["code"], p["description"], money(p["newest"]["payment_limit"]),
              " ".join(p["other_descriptions"] + sorted({x["drug_name"] or "" for x in p["products"]}))]
             for p in code_pages]
    (OUT / "search-index.js").write_text(
        "window.SEARCH_INDEX = " + json.dumps(index, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8")

    env = make_env()
    common = {
        # The Biosimilars link appears once at least one group is published.
        "nav": NAV[:3] + ([("biosimilars.html", "Biosimilars")] if groups else [])
               + [(NCR_PAGE, "NCR calculator")] + NAV[3:],
        # Added to the style.css and script links so browsers fetch them
        # again whenever they change instead of using an old saved copy.
        "asset_version": hashlib.sha256(STYLESHEET.read_bytes() + CHART_SCRIPT.read_bytes()
                                        + NCR_SCRIPT.read_bytes() + MAP_SCRIPT.read_bytes()).hexdigest()[:10],
        "latest_q": latest_q,
        "source_page": manifest["source_page"],
        "top_n": TOP_N,
        "latest_posted": summary["posted"],
        "pct_min_prior": PCT_MIN_PRIOR,
        "n_codes_all": len(code_pages),
        "rebate_page": REBATE_PAGE,
    }

    def write(path: str, template: str, **context) -> None:
        root = "../" * path.count("/")
        page = env.get_template(template).render(root=root, current_page=path, **common, **context)
        (OUT / path).parent.mkdir(parents=True, exist_ok=True)
        (OUT / path).write_text(page, encoding="utf-8")

    write("index.html", "index.html", s=summary, lead=lead, around=around, co=coinsurance)
    write("coinsurance.html", "coinsurance.html", co=coinsurance)
    if groups:
        (OUT / "biosimilars").mkdir()
        write("biosimilars.html", "biosimilars.html", groups=groups, groups_source=groups_source)
        for grp in groups:
            write(f"biosimilars/{grp['slug']}.html", "biosimilar_group.html", g=grp, groups=groups,
                  chart_labels=[quarter_label(q) for q in recent_qs], groups_source=groups_source)
    write("movers.html", "movers.html", s=summary, movers=movers, n_below_floor=n_below_floor,
          spark_quarters=SPARK_QUARTERS)
    write("changes.html", "changes.html", s=summary, new_codes=new_codes, dropped_codes=dropped_codes)
    for p in code_pages:
        write(f"codes/{p['code']}.html", "code.html", p=p, co_usual=usual_co)
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
          n_versions=len({(r["source_file"]) for r in rows}))

    ncr_data = {"quarter": quarter_label(latest_q), "markets": ncr_markets}
    write(NCR_PAGE, "ncr_calculator.html", ncr_data=ncr_data, sales_q=quarter_offset(latest_q, -2))
    ncr_codes = {p["hcpcs"] for m in ncr_markets for p in m["products"] if p["status"] == "cms"}

    write("about.html", "about.html", sales_q=quarter_offset(latest_q, -2),
          last_download=last_download.date(), built=date.today(), first_q=quarters[0])

    n_pages = sum(1 for _ in OUT.rglob("*.html"))
    print(f"Built {OUT.relative_to(ROOT)}/ : {n_pages:,} pages, latest quarter {latest_q}")
    print(f"  {summary['n_codes']:,} codes in {latest_q}; {summary['compared']:,} compared with {prior_q}")
    print(f"  up {summary['up']:,}, down {summary['down']:,}, unchanged {summary['same']:,}; "
          f"median change {percent(summary['median_pct'])}")
    print(f"  new {summary['n_new']:,}, dropped {summary['n_dropped']:,}")
    print(f"  reduced coinsurance: {len(reduced):,} codes below the usual "
          f"{plain_percent(str(usual_co))} ({usual_co_n:,} codes)")
    print(f"  biosimilar groups published: {len(groups)}"
          + (f" (from {', '.join(groups_source)})" if groups_source else " (no sheet snapshot loaded)"))
    print(f"  NCR calculator: {len(ncr_markets)} markets, {len(ncr_codes)} codes priced from {latest_q}")
    if mismatch:
        print(f"\nNOTE: these codes have a reduced coinsurance percentage or the CMS note "
              f"'{INFLATION_NOTE}', but not both: {', '.join(mismatch)}")
    for w in warnings:
        print(f"\nNOTE: {w}")
    print(f"  left out of comparison: {summary['no_limit']} no limit, "
          f"{summary['unit_changed']} billing unit changed, {summary['zero_prior']} prior limit $0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
