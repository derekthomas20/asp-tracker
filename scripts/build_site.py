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
    codes/XXXXX.html    one page per HCPCS code: chart, products (NDCs in
                        CMS, claim and FDA 10-digit forms), an example dose
                        in billing units (calculator markets with an FDA
                        label dose), a JW/JZ note (codes in CMS's discarded
                        drug file),
                        Medicare usage by year (data.cms.gov files),
                        table, CMS source links
    search.html         find a code by code or drug name
    downloads.html      the full history as one CSV (in downloads/)
    about.html          where the data comes from and what it means
    companies.html      companies that make the drugs, from data/companies.json
    companies/X.html    (verified labeler codes only; see scripts/companies.py):
                        their codes, payment limit changes, and Medicare
                        spending on the codes only they make
    tools/ncr-calculator/index.html
                        biosimilar net cost recovery calculator: products from
                        the sheet, packages from the CMS crosswalk, limits from
                        CMS, default doses from data/ncr_markets.json
    tools/ncr-calculator/products.js
                        every product in the CMS crosswalk, for comparisons
                        visitors build themselves on the calculator page
    sitemap.xml         every page, for search engines, dated by its CMS posting
    robots.txt          lets search engines in and points them to sitemap.xml
    search-index.js    the list behind the header search suggestions
Also build/newsletter.json (not part of the site): the subject and Markdown
body of a newsletter for the latest quarter, from site/templates/newsletter.md.
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

import companies as company_data  # data/companies.json and its FDA/GLEIF/SEC checks

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
SITE_INFO = ROOT / "data" / "site.json"  # hand-maintained: who runs the site, contact, sign-up
# The newsletter draft for the latest quarter (not published; see
# scripts/newsletter_draft.py, which saves it to Buttondown as a draft).
NEWSLETTER = ROOT / "build" / "newsletter.json"
NEWSLETTER_ROWS = 5  # increases and decreases listed in the newsletter
NCR_PAGE = "tools/ncr-calculator/index.html"
STATIC = ROOT / "site" / "static"  # logo and favicons
OUT = ROOT / "public"
CSV_NAME = "asp-payment-limits-history.csv"

TOP_N = 25  # rows in each movers table
TOP_DRUGS = 10  # rows in the home page "Largest Part B drugs" list
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
JW_FAQ = ("https://www.cms.gov/medicare/medicare-fee-for-service-payment/hospitaloutpatientpps/"
          "downloads/jw-modifier-faqs.pdf")
SITE_URL = "https://therxcurve.com/"  # canonical addresses, Open Graph links and sitemap.xml
REBATE_PAGE = ("https://www.cms.gov/initiatives/medicare-prescription-drug-affordability/"
               "overview/medicare-inflation-rebate-program")

NAV = [
    ("index.html", "Home"),
    ("movers.html", "Movers"),
    ("changes.html", "New & dropped"),
    ("companies.html", "Companies"),
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
    """CMS counts (claims, units, services, patients) as whole numbers with
    commas: '1,733,604'. CMS gives some unit totals with a decimal place
    ('13,002,343.6'); for display those are rounded to the nearest whole
    number. The database keeps the exact value."""
    if value is None:
        return "N/A"
    return f"{Decimal(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP):,f}"


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
        ref_entry["color_var"] = "--color-ink"
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


MAX_PRODUCT_NAMES = 2  # more names than this: keep the CMS descriptor instead


def clean_brand(text: str | None) -> str:
    """Spending-file brand text -> a display name: 'KEYTRUDA' -> 'Keytruda',
    'PROLIA*' -> 'Prolia', 'AMCHOPLAST, PER SQ CM**' -> 'Amchoplast'.
    The '*' marks in CMS's file mean "more than one product"."""
    t = (text or "").replace("*", "").strip()
    t = re.split(r",\s*per\b", t, flags=re.IGNORECASE)[0].strip()
    return t.title() if t.isupper() else t


def load_product_names(con) -> tuple[dict[str, str], dict[str, str]]:
    """A product name for each code, and the Part B Spending file's generic
    name for each code ('pembrolizumab'), from CMS files only. Product names:

      1. the NDC-HCPCS crosswalk drug names for the newest quarter the code
         appears in (names differing only in capitals are one name; a name
         that is another listed name plus more words, such as
         'Dopamine in D5W' next to 'Dopamine', is dropped). One or two names
         are shown, joined with ' / ': brand names before the generic name,
         and the name matching the Part B Spending file's brand first. With
         more names, the code gets none.
      2. otherwise the Part B Spending by Drug brand name (newest release),
         cleaned by clean_brand().

    Codes with no name here are shown by their CMS short descriptor."""
    brands: dict[str, str] = {}
    generics: dict[str, str] = {}
    for code, brand, generic in con.execute("""
            SELECT hcpcs_code, brand_name, generic_name FROM (
                SELECT hcpcs_code, brand_name, generic_name, period_year AS y FROM part_b_spending_quarterly WHERE is_latest_release
                UNION ALL
                SELECT hcpcs_code, brand_name, generic_name, data_year AS y FROM part_b_spending WHERE is_latest_release)
            WHERE brand_name <> ''
            QUALIFY row_number() OVER (PARTITION BY hcpcs_code ORDER BY y DESC) = 1""").fetchall():
        brands[code] = brand
        generics[code] = generic
    crosswalk: dict[str, list[str]] = {}
    for code, names in con.execute("""
            SELECT hcpcs_code, list(DISTINCT drug_name ORDER BY drug_name) FROM crosswalks
            WHERE is_current AND hcpcs_code IS NOT NULL AND trim(coalesce(drug_name, '')) <> ''
            GROUP BY hcpcs_code, effective_quarter
            QUALIFY effective_quarter = max(effective_quarter) OVER (PARTITION BY hcpcs_code)""").fetchall():
        crosswalk[code] = names

    out = {}
    for code in set(brands) | set(crosswalk):
        brand = clean_brand(brands.get(code))
        generic = clean_brand(generics.get(code)).lower()
        names = {}
        for n in crosswalk.get(code, []):
            n = display_text(n)
            names.setdefault(n.lower(), n if not n.isupper() or len(n) <= 4 else n.title())
        keys = [k for k in names if not any(k != o and k.startswith(o + " ") for o in names)]
        if keys:
            if len(keys) > MAX_PRODUCT_NAMES:
                continue
            # Brand names before the generic name; the spending file's brand first.
            keys.sort(key=lambda k: (k == generic, k != brand.lower(), k))
            out[code] = " / ".join(names[k] for k in keys)
        elif brand:
            out[code] = brand
    return out, {code: clean_brand(g).lower() for code, g in generics.items() if clean_brand(g)}


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
# Companies: who makes each code (data/companies.json, checked against the
# FDA NDC Directory, GLEIF and SEC by scripts/companies.py)
# --------------------------------------------------------------------------

HOW_VERIFIED = {
    company_data.AGREES: "GLEIF links the FDA-listed company to it",
    company_data.NAME_MATCH: "the FDA-listed name includes the company name",
    company_data.APPROVED: "checked by the site owner",
}


def company_tickers(c: dict, sec: dict | None) -> list[dict]:
    """Tickers to show: the home-market listing (typed in companies.json),
    then the US one. A US ticker is shown only when SEC's list has it under
    the company's CIK (SEC's current ticker is used), or for an OTC ADR,
    which SEC does not list. A ticker SEC no longer lists is left out until
    companies.json is fixed (companies_report.py flags it)."""
    out = []
    if c["home_listing"]:
        out.append(c["home_listing"])
    us = c["us_listing"]
    listed = sec.get(c["sec_cik"], []) if sec and c["sec_cik"] else []
    if listed:
        pick = next((r for r in listed if us and r["ticker"] == us["ticker"]), listed[0])
        out.append({"ticker": pick["ticker"], "exchange": pick["exchange"] or (us or {}).get("exchange")})
    elif us and us["exchange"] == "OTC" and not c["sec_cik"]:
        out.append(us)
    return out


def load_makers(con, quarter: str, warn) -> dict | None:
    """Which companies make each HCPCS code in the quarter's ASP NDC-HCPCS
    crosswalk. A row counts only when it is credited (by name or labeler
    code, see scripts/companies.py) to a company through a verified labeler
    code. Returns {companies, makers: {code: {company ids}}, unknown: {code:
    {labeler names not credited}}}, or None when the FDA or GLEIF snapshot
    is missing."""
    doc = company_data.load_companies()
    fda, gleif, sec = company_data.load_fda_labelers(), company_data.load_gleif(), company_data.load_sec()
    if fda is None or gleif is None:
        warn("no FDA or GLEIF snapshot in reference/company_sources/; company pages not built "
             "(run scripts/fetch_company_sources.py).")
        return None
    status = {}
    for c in doc["companies"]:
        for e in c["labeler_codes"]:
            status[e["code"]] = company_data.check_labeler(doc, c, e, fda, gleif)[0]
    published = {doc["_code_owner"][code] for code, s in status.items() if s in company_data.VERIFIED}

    rows = con.execute("""
        SELECT hcpcs_code, ndc, labeler_name FROM crosswalks
        WHERE crosswalk_type = 'ASP' AND is_current AND effective_quarter = ?
    """, [quarter]).fetchall()
    if not rows:
        warn(f"no {quarter} ASP crosswalk; company pages not built.")
        return None
    makers: dict[str, set[str]] = {}
    unknown: dict[str, set[str]] = {}
    for hcpcs, ndc, name in rows:
        if company_data.SKIN_SUBSTITUTE.match(hcpcs):
            continue
        m = company_data.NDC_5_4_2.match(ndc or "")
        labeler = m.group(1) if m else None
        cid, reason = company_data.credit_row(doc, labeler, name or "")
        credited = cid is not None and ((reason == "code" and status.get(labeler) in company_data.VERIFIED)
                                        or (reason == "name" and cid in published))
        if credited:
            makers.setdefault(hcpcs, set()).add(cid)
        elif reason != "not a manufacturer":
            unknown.setdefault(hcpcs, set()).add(display_text(name))

    companies = {}
    for c in doc["companies"]:
        if c["id"] not in published:
            continue
        companies[c["id"]] = {
            "id": c["id"], "name": c["name"], "tickers": company_tickers(c, sec),
            "labelers": [{"code": e["code"], "fda_names": sorted(fda.get(e["code"], [])),
                          "how": HOW_VERIFIED[status[e["code"]]]}
                         for e in c["labeler_codes"] if status[e["code"]] in company_data.VERIFIED],
        }
    return {"companies": companies, "makers": makers, "unknown": unknown}


BRIEF_NAMES = 3  # drugs named in the "What this means" biosimilar sentence
# A company's spending change vs. the year before is shown only when the codes
# with spending in both years make up at least this percent of this year's.
MIN_CHANGE_COVERAGE = Decimal(50)


def company_brief(own: list[dict], total: Decimal | None, total_part_b: Decimal | None,
                  effect_by_code: dict[str, Decimal | None]) -> dict:
    """Figures for the plain-language "What this means" box on a company
    page, all from the company's own codes (rows sorted by spending, largest
    first). A figure that can't be computed is None and its sentence is left
    out of the page.

      share_of_part_b  the company's spending as a percent of all Part B drug spending
      price_trend      this quarter's payment limit change, averaged with each
                       code weighted by its Medicare spending
      effect           the Movers "yearly effect" (change per billing unit times
                       the code's Medicare units in the latest final year), summed
      top              the largest drug and its share of the company's spending
      biosimilar       spending on reference products that face biosimilars"""
    weighted = [r for r in own if r["qoq"] is not None and r["spending"]]
    weight = sum(r["spending"] for r in weighted)
    effects = [effect_by_code[r["code"]] for r in own if effect_by_code.get(r["code"]) is not None]
    facing = [r for r in own if r["group"] and r["group"]["ref"]["code"] == r["code"] and r["spending"]]
    return {
        "share_of_part_b": share(total, total_part_b) if total else None,
        "price_trend": sum(r["qoq"] * r["spending"] for r in weighted) / weight if weight else None,
        "n_up": sum(1 for r in own if r["qoq"] is not None and r["qoq"] > 0),
        "n_down": sum(1 for r in own if r["qoq"] is not None and r["qoq"] < 0),
        "effect": sum(effects) if effects else None,
        "n_effect": len(effects),
        "top": ({"row": own[0], "share": share(own[0]["spending"], total)}
                if total and len(own) > 1 and own[0]["spending"] else None),
        "biosimilar": ({"spending": sum(r["spending"] for r in facing),
                        "share": share(sum(r["spending"] for r in facing), total),
                        "names": [r["name"] for r in facing[:BRIEF_NAMES]], "n": len(facing)}
                       if facing and total else None),
    }


def build_companies(mk: dict, latest: dict, code_pages: list[dict], usage, usage_years, group_of,
                    recent_limits, effect_by_code: dict[str, Decimal | None]) -> list[dict]:
    """One entry per published company that makes at least one code in the
    latest quarter. A code is the company's own ("only maker") when every
    crosswalk row for it is credited to that company; Medicare spending is
    added up only for those codes. Codes shared with other makers, or with
    labelers not assigned to a company yet, are listed without spending."""
    pages = {p["code"]: p for p in code_pages}
    year = max(usage_years) if usage_years else None
    prev_year = year - 1 if year and (year - 1) in usage_years else None
    # All Medicare Part B drug spending that year, for each company's share.
    total_part_b = sum((u[year]["spending"] for u in usage.values()
                        if year in u and u[year]["spending"] is not None), Decimal(0)) if year else None
    out = []
    for cid, c in mk["companies"].items():
        own, shared = [], []
        for code in sorted(latest):
            ids = mk["makers"].get(code, set())
            if cid not in ids:
                continue
            p = pages[code]
            spend = usage.get(code, {}).get(year, {}).get("spending") if year else None
            spend_prev = usage.get(code, {}).get(prev_year, {}).get("spending") if prev_year else None
            row = {"code": code, "name": p["name"], "product": p["product"], "description": p["description"],
                   "dosage": p["dosage"], "limit": p["newest"]["payment_limit"], "qoq": p["qoq"], "yoy": p["yoy"],
                   "spark": Markup(sparkline(recent_limits(code))), "spending": spend, "spending_prev": spend_prev, "group": group_of.get(code)}
            others = sorted(mk["companies"][o]["name"] for o in ids - {cid})
            n_unknown = len(mk["unknown"].get(code, ()))
            if others or n_unknown:
                row["others"] = others
                row["n_unknown"] = n_unknown
                shared.append(row)
            else:
                own.append(row)
        if not own and not shared:
            continue
        own.sort(key=lambda r: (-(r["spending"] or 0), r["code"]))
        shared.sort(key=lambda r: r["code"])
        with_spend = [r for r in own if r["spending"] is not None]
        total = sum(r["spending"] for r in with_spend) if with_spend else None
        # Change on the same codes: only codes with spending in both years.
        both = [r for r in own if r["spending"] is not None and r["spending_prev"] is not None]
        prev_total = sum(r["spending_prev"] for r in both) if both else None
        cur_same = sum(r["spending"] for r in both) if both else None
        # The change is shown only when the codes in both years are most of this
        # year's spending: otherwise a few small codes would speak for the
        # company (Baxter's biggest code is new, so its change covered <1%).
        coverage = share(cur_same, total) if both else None
        change = (share(cur_same - prev_total, prev_total)
                  if coverage is not None and coverage >= MIN_CHANGE_COVERAGE else None)
        # Spending on the codes it shares, all makers together: CMS reports
        # spending per code, not per company, so it is never split.
        shared_spend = [r["spending"] for r in shared if r["spending"] is not None]
        shared_total = sum(shared_spend) if shared_spend else None
        listed = own + shared
        out.append({
            **c, "own": own, "shared": shared,
            "spending": total, "spending_change": change, "change_coverage": coverage,
            "shared_spending": shared_total,
            "mostly_shared": shared_total is not None and shared_total > (total or 0),
            "brief": company_brief(own, total, total_part_b, effect_by_code),
            "n_up": sum(1 for r in listed if r["qoq"] is not None and r["qoq"] > 0),
            "n_down": sum(1 for r in listed if r["qoq"] is not None and r["qoq"] < 0),
            "groups": sorted({r["group"]["slug"]: r["group"] for r in listed if r["group"]}.values(),
                             key=lambda g: g["molecule"]),
        })
    out.sort(key=lambda c: (-(c["spending"] or 0), c["name"].lower()))
    return out


# --------------------------------------------------------------------------
# NCR calculator: products from the biosimilar groups sheet, packages from
# the CMS crosswalk, limits from the CMS payment limit file, default doses
# from data/ncr_markets.json
# --------------------------------------------------------------------------

DOSE_BASIS = {"per kg": "mgkg", "per m2": "mgm2", "flat": "flat"}  # names used in site/ncr.js
UNIT_NAMES = {"MG": "mg", "MCG": "mcg", "UNIT": "units", "UNITS": "units", "IU": "IU", "GM": "g",
              "ML": "mL", "CC": "cc"}
CATALOG_SCRIPT = "tools/ncr-calculator/products.js"


def billing_unit(text: str | None) -> tuple[Decimal, str] | None:
    """(size, measure) of one billing unit from CMS dosage text such as
    '10 MG', '0.5 MG', '1 MCG', '1000 UNITS', '1 IU' or '1 ML'. None if not
    in that form."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(MG|MCG|UNITS?|IU|GM|ML|CC)\s*", text or "", re.IGNORECASE)
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
                                 "in the form '<number> MG', 'MCG', 'UNITS', 'IU', 'GM' or 'ML'.")
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


def dose_examples(markets: list[dict]) -> dict[str, list[dict]]:
    """A worked dose for code pages, for each code in a calculator market
    whose default dose names its FDA label in data/ncr_markets.json (doses
    marked as prototype defaults are left out). The dose is turned into
    billing units of the code's own CMS unit, rounded up to whole units as
    the calculator does. {code: [{source, basis, value, weight, bsa, unit}]}."""
    with NCR_MARKETS.open(encoding="utf-8") as f:
        config = {m["id"]: m for m in json.load(f, parse_float=Decimal)["markets"]}
    out: dict[str, list[dict]] = {}
    for mk in markets:
        cfg = config[mk["id"]]
        if "label" not in cfg["dose_source"].lower():
            continue
        dose = cfg["dose"]
        for code in sorted({p["hcpcs"] for p in mk["products"]}):
            out.setdefault(code, []).append({
                "source": cfg["dose_source"], "basis": dose["basis"], "value": Decimal(dose["value"]),
                "weight": Decimal(dose["weight_kg"]), "bsa": Decimal(dose["bsa_m2"]), "measure": mk["unit_name"]})
    return out


def worked_dose(ex: dict, dosage: str, limit: Decimal | None) -> dict | None:
    """One dose example in this code's billing unit: amount, billing units
    (rounded up), and the payment limit for those units."""
    bu = billing_unit(dosage)
    if bu is None or bu[1] != ex["measure"]:
        return None
    size, measure = bu
    amount = ex["value"] * {"per kg": ex["weight"], "per m2": ex["bsa"], "flat": Decimal(1)}[ex["basis"]]
    exact = amount / size
    units = exact.to_integral_value(rounding="ROUND_CEILING")
    return {**ex, "amount": amount, "units": units, "rounded": units != exact,
            "at_limit": units * limit if limit is not None else None, "unit_measure": measure}


def build_ncr_catalog(latest: dict[str, dict], packages: dict[str, set], groups: list[dict],
                      markets: list[dict]) -> list[dict]:
    """Every product in the latest CMS crosswalk, for visitors who build their
    own comparison: one product per HCPCS code and crosswalk drug name, with
    each package size, its CMS payment limit and its billing unit. A product
    that is also in a market keeps that market's name, default dose, packages,
    multi-dose vials and example prices; any other product has no default
    dose (CMS files don't give one) and its packages start as single-dose.
    A code whose billing unit is not an amount ('PER DOSE', '1 EA') is dosed
    in billing units."""
    role = {e["code"]: e["role"] for g in groups for e in [g["ref"], *g["bios"]]}
    in_market = {}
    for mk in markets:
        for p in mk["products"]:
            in_market.setdefault(p["key"], (mk, p))
    out = []
    for code, items in sorted(packages.items()):
        r = latest.get(code)
        dosage = r["hcpcs_dosage"] if r else None
        bu = billing_unit(dosage)
        size, measure = bu or (Decimal(1), "billing units")
        for name in sorted({n for n, _, _ in items}):
            key = slugify(f"{name} {code}")
            mk, mp = in_market.get(key, (None, None))
            # A market package's key is "<billing units>-<SDV or MDV>".
            market_pkgs = {k["key"].rsplit("-", 1)[0]: k for k in mp["packages"]} if mp else {}
            pkgs = []
            for units in sorted({u for n, u, _ in items if n == name}):
                k = market_pkgs.get(number_text(units))
                # A market's package keeps its vial type; one the market leaves out starts unused.
                pkgs.append({"key": number_text(units), "amount": float(units * size), "units": float(units),
                             "type": k["type"] if k else "OFF" if mp else "SDV",
                             "example_price": k["example_price"] if k else None})
            limit = r["payment_limit"] if r else None
            out.append({
                "key": key,
                "name": mp["name"] if mp else name,
                "hcpcs": code,
                "description": display_text(r["short_description"]) if r else "",
                "role": role.get(code, ""),
                "reference": role.get(code) == "reference",
                "status": "missing" if r is None else "cms" if limit is not None else "no_limit",
                "cms_limit": plain_decimal(limit) if limit is not None else None,
                "cms_dosage": dosage or "",
                "unit_size": float(size), "unit_name": measure,
                "unit_label": f"{size.normalize():,f} {measure}" if bu else (dosage or "1 billing unit"),
                "dose": {k: mk["dose"][k] for k in ("basis", "value")} if mk else None,
                "example_rebate_pct": mp["example_rebate_pct"] if mp else 0,
                "packages": pkgs,
            })
    return out


# --------------------------------------------------------------------------
# Page titles and descriptions for search engines and link previews. They
# are written from the same figures the page shows, never typed in.
# --------------------------------------------------------------------------

def seo(title: str, description: str, jsonld: dict | None = None) -> dict:
    return {"title": title, "description": " ".join(description.split()), "jsonld": jsonld}


def quarter_dates(q: str) -> tuple[date, date]:
    """'2026Q4' -> (2026-10-01, 2026-12-31)."""
    year, last_month = int(q[:4]), int(q[5]) * 3
    return (date(year, last_month - 2, 1),
            date(year, last_month, calendar.monthrange(year, last_month)[1]))


def dataset(name: str, description: str, url: str, first_q: str, last_q: str,
            modified: date | None, keywords: list[str], measured: str, source_page: str) -> dict:
    """schema.org Dataset markup. No license is given: the CMS files don't
    state one, and Rxcurve doesn't claim one for them."""
    return {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": name, "description": description, "url": url,
        "temporalCoverage": f"{quarter_dates(first_q)[0]}/{quarter_dates(last_q)[1]}",
        "variableMeasured": measured,
        "keywords": keywords,
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "Rxcurve", "url": SITE_URL},
        "isBasedOn": source_page,
        **({"dateModified": modified.isoformat()} if modified else {}),
        "distribution": [{"@type": "DataDownload", "encodingFormat": "text/csv",
                          "contentUrl": f"{SITE_URL}downloads/{CSV_NAME}"}],
    }


def page_url(path: str) -> str:
    """The address search engines should use: '' for the home page,
    'tools/ncr-calculator/' for a folder's index.html."""
    return SITE_URL + (path[:-len("index.html")] if path.endswith("index.html") else path)


def code_seo(p: dict, generic: str | None, source_page: str) -> dict:
    """'J9271 Keytruda (pembrolizumab): Medicare Part B payment limit, 4Q26'."""
    name = p["name"]
    # CMS abbreviates some generic names ('betamethasone acetate,sod phos'); those are left out.
    if p["product"] and generic and not re.search(r"[/,]", generic) and generic not in p["product"].lower():
        name += f" ({generic})"
    q, limit = quarter_label(p["last_q"]), p["newest"]["payment_limit"]
    title = f"{p['code']} {name}: Medicare Part B payment limit, {q}"
    since = f"History since {quarter_label(p['first_q'])}, from the CMS ASP pricing files."
    if limit is None:
        price = f"CMS gives no Medicare Part B payment limit for {q}."
    else:
        price = f"Medicare Part B payment limit, {q}: {money(limit)} per {p['dosage']}"
        price += f" ({percent(p['qoq'])} vs. last quarter)." if p["qoq"] is not None else "."
    lead = f"{p['code']} {name}." if p["in_latest"] else f"{p['code']} {name}, last listed by CMS in {q}."
    description = f"{lead} {price} {since}"
    return seo(title, description,
               dataset(title, description, page_url(f"codes/{p['code']}.html"), p["first_q"], p["last_q"],
                       p["newest"]["cms_posted_date"], [p["code"], p["name"], "HCPCS", "ASP", "Medicare Part B"],
                       f"Medicare Part B payment limit per {p['dosage']}", source_page))


def company_seo(c: dict, cs: dict, q: str) -> dict:
    tickers = " / ".join(t["ticker"] for t in c["tickers"])
    title = f"{c['name']}{f' ({tickers})' if tickers else ''}: Medicare Part B drugs and payment limits, {q}"
    n = len(c["own"]) + len(c["shared"])
    description = f"{n:,} Medicare Part B drug code{'s' if n != 1 else ''} from {c['name']} in the CMS {q} file"
    if c["spending"] is not None:
        description += (f", and {big_money(c['spending'])} in Medicare Part B spending in {cs['year']}"
                        f"{' (preliminary)' if cs['preliminary'] else ''} on drugs only {c['name']} makes")
    if c["mostly_shared"]:
        description += (f". {'Most Medicare spending on its drugs is on codes' if c['own'] else 'All its codes are'} "
                        f"shared with other makers: {big_money(c['shared_spending'])} in {cs['year']}, "
                        "all makers together")
    trend = c["brief"]["price_trend"]
    if trend is not None:
        description += f". Payment limits {percent(trend)} this quarter, weighted by spending"
    return seo(title, description + ".")


def group_seo(g: dict, q: str) -> dict:
    ref, n = g["ref"], len(g["bios"])
    title = f"{g['molecule']} biosimilars vs. {ref['name']}: Medicare payment limits, {q}"
    description = (f"{ref['name']} ({ref['code']}) and {n} {g['molecule'].lower()} biosimilar{'s' if n != 1 else ''}: "
                   f"{q} Medicare Part B payment limits per {ref['dosage']}")
    if g["lowest"]:
        description += f". Lowest: {g['lowest']['name']}, {percent(g['lowest']['vs_ref'])} vs. the reference"
    m = g["market"]
    if m and m["latest"]["bio_share"] is not None:
        description += (f". Biosimilars had {share_text(m['latest']['bio_share'])} of Medicare claims "
                        f"in {m['latest']['label']}")
    return seo(title, description + ".")


def write_sitemap(pages: list[tuple[str, date | None]]) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, modified in sorted(pages):
        lines.append(f"<url><loc>{html.escape(page_url(path))}</loc>"
                     + (f"<lastmod>{modified.isoformat()}</lastmod>" if modified else "") + "</url>")
    lines.append("</urlset>")
    (OUT / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}sitemap.xml\n",
                                    encoding="utf-8")


def load_site_info(warn) -> dict:
    """data/site.json: who runs the site and the Buttondown sign-up. Blank
    fields leave their part of the site off (and are reported); a value in
    the wrong form stops the build, since it goes into links."""
    with SITE_INFO.open(encoding="utf-8") as f:
        info = {k: v.strip() for k, v in json.load(f).items() if not k.startswith("_")}
    where = SITE_INFO.relative_to(ROOT)
    user = info["buttondown_username"]
    if user and not re.fullmatch(r"[A-Za-z0-9_-]+", user):
        raise SystemExit(f"STOPPED: {where}: buttondown_username {user!r} should be just the username.")
    email = info["contact_email"]
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise SystemExit(f"STOPPED: {where}: contact_email {email!r} is not an email address.")
    token = info.setdefault("cloudflare_analytics_token", "")
    if token and not re.fullmatch(r"[0-9a-f]{32}", token):
        raise SystemExit(f"STOPPED: {where}: cloudflare_analytics_token {token!r} should be the 32-character token.")
    if not user:
        warn(f"{where}: no buttondown_username, so the email sign-up form is shown switched off.")
    blank = [k for k in ("owner_name", "owner_bio", "contact_email") if not info[k]]
    if blank:
        warn(f"{where}: {', '.join(blank)} not filled in, so the About page has no 'Who runs Rxcurve' section.")
    info["show_owner"] = not blank
    return info


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
                       plain_num=lambda v: f"{v.normalize():,f}",  # Decimal 400.0 -> '400', 1.90 -> '1.9'
                       md=lambda t: re.sub(r"([\\`*_\[\]])", r"\\\1", str(t)),  # Markdown-safe text
                       big_money=big_money, short_money=short_money, share=share_text, count=count,
                       short_signed_money=lambda v: sign(v) + short_money(abs(v)))
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

    warnings: list[str] = []
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
    product_names, generic_names = load_product_names(con)
    makers = load_makers(con, latest_q, warnings.append)
    fda_packages = company_data.load_fda_packages()
    if fda_packages is None:
        warnings.append("no FDA package snapshot in reference/company_sources/; code pages show no 10-digit "
                        "NDCs (run scripts/fetch_company_sources.py --only fda_packages).")
    con.close()
    if CMS_DATA_MANIFEST.exists():
        with CMS_DATA_MANIFEST.open(encoding="utf-8") as f:
            for e in json.load(f)["files"]:
                cms_url[e["local_path"]] = e["url"]

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
    def named(d: dict) -> dict:
        """Add 'product' (CMS product name, or None) and 'name' (product name,
        else the CMS short descriptor) to a row that has code and description."""
        d["product"] = product_names.get(d["code"])
        d["name"] = d["product"] or d["description"]
        return d

    cmp = compare(latest, prior)
    for c in cmp["compared"]:
        named(c)
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
        return named({"code": r["hcpcs_code"], "description": display_text(r["short_description"]),
                      "dosage": r["hcpcs_dosage"], "limit": r["payment_limit"]})

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
    ncr_catalog = build_ncr_catalog(latest, ncr_packages, groups, ncr_markets)
    dose_by_code = dose_examples(ncr_markets)
    group_of = {}
    for grp in groups:
        for e in [grp["ref"], *grp["bios"]]:
            group_of[e["code"]] = grp
    groups_source = sorted({r["source_file"] for r in group_rows})

    # Newest year with final (yearly-file) units; the Movers "yearly effect" uses it.
    final_years = [y for y, info in usage_years.items() if not info["preliminary"]]
    effect_year = max(final_years) if final_years else None

    # ---- Movers: yearly effect at the latest final year's Medicare use ------
    # Change per billing unit times the code's units that year, only when the
    # code was billed in the same unit then as in both compared quarters.
    for c in compared:
        row = usage.get(c["code"], {}).get(effect_year) if effect_year else None
        unit_now = " ".join((c["dosage"] or "").upper().split())
        same = units_in_year.get((c["code"], effect_year)) == {unit_now}
        c["units_year"] = row["units"] if row and row["units"] is not None and same else None
        c["effect"] = c["change"] * c["units_year"] if c["units_year"] is not None else None
    with_effect = [c for c in compared if c["effect"]]
    movers["effect"] = {
        "increases": sorted((c for c in with_effect if c["effect"] > 0), key=lambda c: -c["effect"])[:TOP_N],
        "decreases": sorted((c for c in with_effect if c["effect"] < 0), key=lambda c: c["effect"])[:TOP_N],
    }

    # ---- Home page: largest Part B drugs by Medicare spending --------------
    top_year = max(usage_years) if usage_years else None
    all_codes = {r["hcpcs_code"] for r in rows}  # codes that have a page on the site
    newest_desc = {r["hcpcs_code"]: r["short_description"] for r in current}  # rows are in quarter order
    top_drugs = sorted(((code, by_year[top_year]) for code, by_year in usage.items()
                        if code in all_codes and top_year in by_year and by_year[top_year]["spending"] is not None),
                       key=lambda x: (-x[1]["spending"], x[0]))[:TOP_DRUGS]
    top_drugs = {"year": top_year, "preliminary": bool(top_year and usage_years[top_year]["preliminary"]),
                 "rows": [{"code": code, "spending": r["spending"], "claims": r["claims"],
                           "description": display_text(newest_desc.get(code))}
                          for code, r in top_drugs]} if top_year else None
    for r in (top_drugs or {}).get("rows", []):
        named(r)

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
            # CMS writes NDCs as 11 digits, 5-4-2. Claims use the same 11 digits
            # without hyphens; the 10-digit form is the one FDA lists, if any.
            is_542 = bool(company_data.NDC_5_4_2.match(prod["ndc"] or ""))
            code_products.append({**prod, "per_package": per_pkg,
                                  "ndc_claim": prod["ndc"].replace("-", "") if is_542 else None,
                                  # FDA sometimes lists one package under two product listings; shown once.
                                  "fda": list({f["ndc_10"]: f for f in (fda_packages or {}).get(prod["ndc"], [])
                                               }.values()) if is_542 else []})
            sources.setdefault(prod["source_file"], {
                "quarter": latest_q, "is_current": True, "posted": prod["cms_posted_date"],
                "source_name": prod["source_file"].rsplit("/", 1)[-1],
                "source_url": cms_url.get(prod["source_file"]),
            })
        code_pages.append({
            "code": code,
            "description": display_text(newest["short_description"]),
            "product": product_names.get(code),
            "name": product_names.get(code) or display_text(newest["short_description"]),
            "generic": generic_names.get(code),
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
            "doses": [d for d in (worked_dose(ex, newest["hcpcs_dosage"], newest["payment_limit"])
                                  for ex in dose_by_code.get(code, []) if code in latest) if d],
        })
        u = code_pages[-1]["usage"]
        if u:
            u["sources"] = [{"name": f.rsplit("/", 1)[-1], "url": cms_url.get(f)} for f in u["source_files"]]

    # ---- Companies ---------------------------------------------------------
    effect_by_code = {c["code"]: c["effect"] for c in compared}
    company_list = (build_companies(makers, latest, code_pages, usage, usage_years, group_of, recent_limits,
                                    effect_by_code) if makers else [])
    company_of = {c["id"]: c for c in company_list}
    for p in code_pages:
        ids = makers["makers"].get(p["code"], set()) if makers and p["in_latest"] else set()
        p["makers"] = sorted((company_of[i] for i in ids if i in company_of), key=lambda c: c["name"].lower())
        p["n_unknown_makers"] = len(makers["unknown"].get(p["code"], ())) if makers and p["in_latest"] else 0
    for c in compared:
        ids = makers["makers"].get(c["code"], set()) if makers else set()
        sole = len(ids) == 1 and not makers["unknown"].get(c["code"])
        c["maker"] = company_of.get(next(iter(ids))) if sole else None
    spend_year = max(usage_years) if usage_years else None
    company_summary = {
        "year": spend_year,
        "preliminary": bool(spend_year and usage_years[spend_year]["preliminary"]),
        "prev_year": spend_year - 1 if spend_year and (spend_year - 1) in usage_years else None,
        "effect_year": effect_year,
        "prior_q": prior_q,
        "n_own": sum(len(c["own"]) for c in company_list),
        "n_shared": len({r["code"] for c in company_list for r in c["shared"]}),
        "n_unassigned": sum(1 for code in latest
                            if makers and makers["unknown"].get(code) and not makers["makers"].get(code)),
    }

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
    # Companies come first, with their page as a fifth item.
    index = [[" ".join(t["ticker"] for t in c["tickers"]) or "Company", c["name"], "Company",
              " ".join([c["id"]] + [t["ticker"] for t in c["tickers"]]), f"companies/{c['id']}.html"]
             for c in company_list]
    index += [[p["code"], p["name"], money(p["newest"]["payment_limit"]),
               " ".join([p["description"]] + p["other_descriptions"]
                        + sorted({x["drug_name"] or "" for x in p["products"]})
                        + [m["name"] for m in p["makers"]])]
              for p in code_pages]
    (OUT / "search-index.js").write_text(
        "window.SEARCH_INDEX = " + json.dumps(index, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8")

    env = make_env()
    # The Companies link appears once a company is published, and the
    # Biosimilars link once a group is; both come before Search.
    nav = [n for n in NAV if n[0] != "companies.html" or company_list]
    k = next(i for i, n in enumerate(nav) if n[0] == "search.html")
    common = {
        "nav": nav[:k] + ([("biosimilars.html", "Biosimilars")] if groups else [])
               + [(NCR_PAGE, "NCR calculator")] + nav[k:],
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
        "jw_faq": JW_FAQ,
        "min_change_coverage": MIN_CHANGE_COVERAGE,
        "site": load_site_info(warnings.append),
        "ncr_page": NCR_PAGE,
    }

    sitemap: list[tuple[str, date | None]] = []

    def write(path: str, template: str, meta: dict, modified: date | None = None, **context) -> None:
        """Render one page. meta is its seo() title and description; modified
        (default: the latest CMS posting) is its sitemap.xml date."""
        root = "../" * path.count("/")
        page = env.get_template(template).render(root=root, current_page=path, meta=meta,
                                                 canonical=page_url(path), **common, **context)
        (OUT / path).parent.mkdir(parents=True, exist_ok=True)
        (OUT / path).write_text(page, encoding="utf-8")
        sitemap.append((path, modified or summary["posted"]))

    q, prior_label, first_label = quarter_label(latest_q), quarter_label(prior_q), quarter_label(quarters[0])
    write("index.html", "index.html",
          seo(f"Medicare Part B drug payment limits, {q}",
              f"Medicare Part B drug payment limits for {q} ({effective_range(latest_q)}), from the CMS ASP "
              f"pricing files: {summary['n_codes']:,} HCPCS codes, {summary['up']:,} up and {summary['down']:,} "
              f"down from {prior_label}. Movers, biosimilars, companies and history since {first_label}."),
          s=summary, lead=lead, around=around, co=coinsurance, top_drugs=top_drugs, has_groups=bool(groups))
    write("coinsurance.html", "coinsurance.html",
          seo(f"Reduced coinsurance Part B drugs, {q}",
              f"{len(reduced):,} Medicare Part B drugs with patient coinsurance below the usual "
              f"{plain_percent(str(usual_co))} in the CMS {q} file, and how much less the patient pays "
              "per billing unit."),
          co=coinsurance)
    write("movers.html", "movers.html",
          seo(f"Biggest Medicare Part B payment limit changes, {q}",
              f"The {TOP_N} largest increases and decreases in Medicare Part B drug payment limits from "
              f"{prior_label} to {q}, by percent, by dollars and by yearly effect on Medicare spending."),
          s=summary, movers=movers, n_below_floor=n_below_floor, spark_quarters=SPARK_QUARTERS,
          effect_year=effect_year, n_effect=len(with_effect))
    write("changes.html", "changes.html",
          seo(f"New and dropped HCPCS codes, {q}",
              f"{summary['n_new']:,} HCPCS codes are new in the CMS {q} Medicare Part B payment limit file "
              f"and {summary['n_dropped']:,} were dropped since {prior_label}."),
          s=summary, new_codes=new_codes, dropped_codes=dropped_codes)
    for p in code_pages:
        write(f"codes/{p['code']}.html", "code.html", code_seo(p, p["generic"], manifest["source_page"]),
              modified=p["newest"]["cms_posted_date"], p=p, co_usual=usual_co)
    write("search.html", "search.html",
          seo("Search Medicare Part B drug codes",
              f"Find any of {len(code_pages):,} HCPCS codes in the CMS ASP pricing files by code or drug name."),
          codes=code_pages)

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
    downloads_meta = seo("Download Medicare Part B ASP payment limit history (CSV)",
                         f"Every Medicare Part B payment limit from {first_label} to {q} in one CSV: "
                         f"{len(rows):,} rows covering {len(code_pages):,} HCPCS codes from "
                         f"{len({r['source_file'] for r in rows}):,} CMS files, revised versions included.")
    downloads_meta["jsonld"] = dataset(
        "Medicare Part B ASP payment limit history", downloads_meta["description"], page_url("downloads.html"),
        quarters[0], latest_q, summary["posted"], ["HCPCS", "ASP", "Medicare Part B", "payment limits"],
        "Medicare Part B payment limit per HCPCS billing unit", manifest["source_page"])
    write("downloads.html", "downloads.html", downloads_meta, csv_name=CSV_NAME, csv_rows=len(rows),
          csv_kb=round(csv_path.stat().st_size / 1024), first_q=quarters[0],
          n_versions=len({(r["source_file"]) for r in rows}))

    if company_list:
        (OUT / "companies").mkdir()
        write("companies.html", "companies.html",
              seo(f"Companies behind Medicare Part B drugs, {q}",
                  f"{len(company_list):,} drug companies with their stock tickers, Medicare Part B spending "
                  f"in {company_summary['year']} on the drugs only they make, and {q} payment limit changes, "
                  "from the CMS NDC–HCPCS crosswalk."),
              companies=company_list, cs=company_summary)
        for c in company_list:
            write(f"companies/{c['id']}.html", "company.html", company_seo(c, company_summary, q),
                  c=c, cs=company_summary, spark_quarters=SPARK_QUARTERS)

    if groups:
        (OUT / "biosimilars").mkdir()
        write("biosimilars.html", "biosimilars.html",
              seo(f"Biosimilar Medicare payment limits vs. reference products, {q}",
                  f"{len(groups)} biosimilar markets ({', '.join(g['molecule'].lower() for g in groups)}): "
                  f"each biosimilar's Medicare Part B payment limit against its reference product, "
                  f"from the CMS {q} file."),
              groups=groups, groups_source=groups_source)
        for grp in groups:
            write(f"biosimilars/{grp['slug']}.html", "biosimilar_group.html", group_seo(grp, q), g=grp, groups=groups,
                  chart_labels=[quarter_label(q) for q in recent_qs], groups_source=groups_source)

    ncr_data = {"quarter": quarter_label(latest_q), "markets": ncr_markets}
    # Every crosswalk product, for comparisons visitors build themselves. A
    # script (not JSON) so the page also works when opened from disk.
    catalog_js = ("window.NCR_CATALOG = " + json.dumps(ncr_catalog, ensure_ascii=False, separators=(",", ":"))
                  + ";\n").encode("utf-8")
    (OUT / CATALOG_SCRIPT).parent.mkdir(parents=True, exist_ok=True)
    (OUT / CATALOG_SCRIPT).write_bytes(catalog_js)
    write(NCR_PAGE, "ncr_calculator.html",
          seo(f"Biosimilar net cost recovery calculator, {q}",
              "Compare what each biosimilar and reference product recovers after acquisition cost, per "
              f"billing unit and per dose, with CMS {q} payment limits filled in. {len(ncr_markets)} markets, "
              f"or build your own comparison from {len(ncr_catalog):,} products."),
          ncr_data=ncr_data, sales_q=quarter_offset(latest_q, -2),
          catalog_version=hashlib.sha256(catalog_js).hexdigest()[:10])
    ncr_codes = {p["hcpcs"] for m in ncr_markets for p in m["products"] if p["status"] == "cms"}

    write("about.html", "about.html",
          seo("About Rxcurve and this data",
              f"Where Rxcurve's Medicare Part B payment limits come from (the CMS ASP pricing files, "
              f"{first_label} to {q}), what a payment limit is, and what it does not mean."),
          sales_q=quarter_offset(latest_q, -2),
          last_download=last_download.date(), built=date.today(), first_q=quarters[0])

    write_sitemap(sitemap)

    # Newsletter draft for the latest quarter, from the same figures as the site.
    first_month = QUARTER_MONTHS[latest_q[5]][0]
    newsletter = env.get_template("newsletter.md").render(
        **common, s=summary, movers=movers, n=NEWSLETTER_ROWS, effect_year=effect_year,
        sales_q=quarter_offset(latest_q, -2), site_url=SITE_URL)
    NEWSLETTER.parent.mkdir(exist_ok=True)
    NEWSLETTER.write_text(json.dumps({
        "quarter": latest_q,
        "subject": f"{first_month} {latest_q[:4]} Medicare Part B payment limits: the biggest changes",
        "body": newsletter.strip() + "\n",
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

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
    print(f"  NCR calculator: {len(ncr_markets)} markets, {len(ncr_codes)} codes priced from {latest_q}; "
          f"{len(ncr_catalog):,} products to build your own comparison from")
    print(f"  companies: {len(company_list)} published; {company_summary['n_own']} codes with one maker, "
          f"{company_summary['n_shared']} shared, {company_summary['n_unassigned']} not assigned yet")
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
