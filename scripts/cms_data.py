"""Load the data.cms.gov usage files into data/asp.duckdb.

Called by scripts/parse.py; not run on its own. Reads every file listed in
data/cms_data_manifest.json (written by scripts/fetch_cms_data.py) and
builds these tables:

    part_b_spending            Medicare Part B Spending by Drug: one row per
                               HCPCS code per calendar year. CMS puts five
                               years side by side in one file; here each
                               year is its own row (data_year).
    part_b_spending_quarterly  Medicare Quarterly Part B Spending by Drug:
                               one row per HCPCS code per period CMS lists
                               (e.g. "2025 (Q1-Q4)" and "2026 (Q1)").
    discarded_drug_units       Medicare Part B Discarded Drug Units: allowed
                               amounts administered vs. discarded, per HCPCS.
                               CMS includes an "Overall" row for all drugs.
    physician_geo_service      Medicare Physician & Other Practitioners by
                               Geography and Service: national and state
                               totals per HCPCS code and place of service
                               (F = facility, O = office).

Every value is stored as CMS wrote it (DECIMAL, not float). Each row keeps
the file it came from, its line number, the date CMS last changed the file
(cms_posted_date) and the release it belongs to (release_period).

Two flags pick "current" rows at query time; no row is ever dropped:
    is_current          the newest version of that release's file
    is_latest_release   is_current, and no newer release covers the same
                        period. Later releases repeat earlier years (each
                        yearly file holds five years), so use this flag to
                        count each year once.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "data" / "cms_data_manifest.json"

DEC = "DECIMAL(38,16)"  # CMS averages run to 14 decimal places
INT = "BIGINT"
TEXT = "VARCHAR"
MAX_DECIMAL_PLACES = 16

SOURCE_COLUMNS = [("release_period", TEXT), ("source_file", TEXT), ("source_row", "INTEGER"),
                  ("cms_posted_date", "DATE"), ("file_version", "INTEGER"), ("is_current", "BOOLEAN"),
                  ("is_latest_release", "BOOLEAN")]


class FormatError(Exception):
    """The file doesn't look like anything we know how to read."""


@dataclass
class Table:
    name: str
    columns: list[tuple[str, str]]  # (column, SQL type), before SOURCE_COLUMNS
    period_column: str  # rows with the same value here cover the same period

    @property
    def all_columns(self) -> list[str]:
        return [c for c, _ in self.columns + SOURCE_COLUMNS]


PART_B = Table("part_b_spending", [
    ("data_year", "INTEGER"), ("hcpcs_code", TEXT), ("hcpcs_description", TEXT),
    ("brand_name", TEXT), ("generic_name", TEXT),
    ("total_spending", DEC), ("total_dosage_units", DEC), ("total_claims", INT),
    ("total_beneficiaries", INT), ("avg_spending_per_dosage_unit", DEC),
    ("avg_spending_per_claim", DEC), ("avg_spending_per_beneficiary", DEC), ("outlier_flag", TEXT),
    # Only on the release's most recent year (CMS gives one value per file):
    ("avg_asp_payment_amount", DEC),  # CMS: average of the year's quarterly ASP file payment amounts
    ("change_avg_spending_per_dosage_unit", DEC),  # vs. the prior year, as a fraction
    ("cagr_avg_spending_per_dosage_unit", DEC), ("cagr_first_year", "INTEGER"),
], period_column="data_year")

PART_B_QUARTERLY = Table("part_b_spending_quarterly", [
    ("period_label", TEXT), ("period_year", "INTEGER"), ("period_quarters", TEXT),
    ("hcpcs_code", TEXT), ("hcpcs_description", TEXT), ("brand_name", TEXT), ("generic_name", TEXT),
    ("total_beneficiaries", INT), ("total_claims", INT), ("total_spending", DEC),
    ("avg_spending_per_beneficiary", DEC), ("avg_spending_per_claim", DEC),
], period_column="period_label")

DISCARDED = Table("discarded_drug_units", [
    ("data_year", "INTEGER"), ("hcpcs_code", TEXT), ("brand_name", TEXT), ("generic_name", TEXT),
    ("total_allowed_amount", DEC), ("total_allowed_administered_amount", DEC),
    ("total_allowed_discarded_amount", DEC), ("pct_administered_units", DEC),
    ("pct_discarded_units", DEC),
], period_column="data_year")

GEO = Table("physician_geo_service", [
    ("data_year", "INTEGER"), ("geo_level", TEXT), ("geo_code", TEXT), ("geo_description", TEXT),
    ("hcpcs_code", TEXT), ("hcpcs_description", TEXT), ("hcpcs_drug_indicator", TEXT),
    ("place_of_service", TEXT), ("total_rendering_providers", INT), ("total_beneficiaries", INT),
    ("total_services", DEC), ("total_beneficiary_day_services", DEC),
    ("avg_submitted_charge", DEC), ("avg_medicare_allowed_amount", DEC),
    ("avg_medicare_payment_amount", DEC), ("avg_medicare_standardized_amount", DEC),
], period_column="data_year")

TABLES = {t.name: t for t in (PART_B, PART_B_QUARTERLY, DISCARDED, GEO)}


# --------------------------------------------------------------------------
# Reading values
# --------------------------------------------------------------------------

def clean(cell: str) -> str:
    return " ".join(cell.replace("\xa0", " ").split())


def decimal(text: str) -> str | None:
    """'1234.5678' -> '1234.5678' (kept as text so nothing is rounded). Blank -> None."""
    t = text.replace(",", "").strip()
    if not t:
        return None
    try:
        d = Decimal(t)
    except InvalidOperation:
        raise FormatError(f"not a number: {text!r}")
    if not d.is_finite() or d.as_tuple().exponent < -MAX_DECIMAL_PLACES:
        raise FormatError(f"more decimal places than the table allows: {text!r}")
    return format(d, "f")


def integer(text: str) -> str | None:
    t = text.replace(",", "").strip()
    if not t:
        return None
    if not re.fullmatch(r"\d+", t):
        raise FormatError(f"not a whole number: {text!r}")
    return t


def expect_header(header: list[str], expected: list[str]) -> None:
    if header != expected:
        unknown = [h for h in header if h not in expected]
        missing = [h for h in expected if h not in header]
        raise FormatError(f"columns changed. Unknown: {unknown}. Missing: {missing}.")


# --------------------------------------------------------------------------
# One reader per dataset. Each yields (line number, row dict).
# --------------------------------------------------------------------------

YEAR_METRICS = {
    "Tot_Spndng": ("total_spending", decimal),
    "Tot_Dsg_Unts": ("total_dosage_units", decimal),
    "Tot_Clms": ("total_claims", integer),
    "Tot_Benes": ("total_beneficiaries", integer),
    "Avg_Spndng_Per_Dsg_Unt": ("avg_spending_per_dosage_unit", decimal),
    "Avg_Spndng_Per_Clm": ("avg_spending_per_claim", decimal),
    "Avg_Spndng_Per_Bene": ("avg_spending_per_beneficiary", decimal),
    "Outlier_Flag": ("outlier_flag", clean),
}


def read_part_b(header, rows, entry):
    """Wide CMS layout (Tot_Spndng_2020 ... Tot_Spndng_2024) -> one row per year."""
    plan = []  # (column index, data year, our column, converter)
    extra_year = {}  # data year -> [(index, our column, converter)]
    for i, h in enumerate(header):
        if h in ("HCPCS_Cd", "HCPCS_Desc", "Brnd_Name", "Gnrc_Name"):
            continue
        if m := re.fullmatch(r"(\w+?)_(20\d\d)", h):
            if m.group(1) in YEAR_METRICS:
                col, conv = YEAR_METRICS[m.group(1)]
                plan.append((i, int(m.group(2)), col, conv))
                continue
        if m := re.fullmatch(r"Avg_DY(\d\d)_ASP_Price", h):
            extra_year.setdefault(2000 + int(m.group(1)), []).append((i, "avg_asp_payment_amount", decimal))
            continue
        if m := re.fullmatch(r"Chg_Avg_Spndng_Per_Dsg_Unt_(\d\d)_(\d\d)", h):
            extra_year.setdefault(2000 + int(m.group(2)), []).append(
                (i, "change_avg_spending_per_dosage_unit", decimal))
            continue
        if m := re.fullmatch(r"CAGR_Avg_Spnd_Per_Dsg_Unt_(\d\d)_(\d\d)", h):
            extra_year.setdefault(2000 + int(m.group(2)), []).append(
                (i, "cagr_avg_spending_per_dosage_unit", decimal))
            extra_year[2000 + int(m.group(2))].append((None, "cagr_first_year", 2000 + int(m.group(1))))
            continue
        raise FormatError(f"unknown column {h!r}")
    for name in ("HCPCS_Cd", "HCPCS_Desc", "Brnd_Name", "Gnrc_Name"):
        if name not in header:
            raise FormatError(f"missing column {name!r}")
    years = sorted({y for _, y, _, _ in plan})
    if len(plan) != len(years) * len(YEAR_METRICS):
        raise FormatError(f"each year should have {len(YEAR_METRICS)} columns; years found: {years}")
    if set(extra_year) - set(years):
        raise FormatError(f"release columns name a year with no data: {sorted(set(extra_year) - set(years))}")

    idx = {h: i for i, h in enumerate(header)}
    for line_no, row in rows:
        base = {"hcpcs_code": clean(row[idx["HCPCS_Cd"]]), "hcpcs_description": clean(row[idx["HCPCS_Desc"]]),
                "brand_name": clean(row[idx["Brnd_Name"]]), "generic_name": clean(row[idx["Gnrc_Name"]])}
        for year in years:
            values = {col: conv(row[i]) for i, y, col, conv in plan if y == year}
            if all(v in (None, "") for v in values.values()):
                continue  # code not billed that year
            for i, col, conv in extra_year.get(year, []):
                values[col] = conv if i is None else conv(row[i])
            yield line_no, {**base, "data_year": year, **values}


def read_part_b_quarterly(header, rows, entry):
    expect_header(header, ["Brnd_Name", "Gnrc_Name", "HCPCS_Cd", "HCPCS_Desc", "Year", "Tot_Benes",
                           "Tot_Clms", "Tot_Spndng", "Avg_Spnd_Per_Bene", "Avg_Spnd_Per_Clm"])
    for line_no, row in rows:
        brand, generic, code, desc, period, benes, claims, spend, per_bene, per_claim = row
        m = re.fullmatch(r"(20\d\d) \((Q[1-4](?:-Q[1-4])?)\)", clean(period))
        if not m:
            raise FormatError(f"line {line_no}: unknown Year value {period!r}")
        yield line_no, {
            "period_label": clean(period), "period_year": int(m.group(1)), "period_quarters": m.group(2),
            "hcpcs_code": clean(code), "hcpcs_description": clean(desc), "brand_name": clean(brand),
            "generic_name": clean(generic), "total_beneficiaries": integer(benes),
            "total_claims": integer(claims), "total_spending": decimal(spend),
            "avg_spending_per_beneficiary": decimal(per_bene), "avg_spending_per_claim": decimal(per_claim),
        }


def read_discarded(header, rows, entry):
    expect_header(header, ["HCPCS_Cd", "Brnd_Name", "Gnrc_Name", "Tot_Mdcr_Alowd_Amt",
                           "Tot_Mdcr_Alowd_Admnrd_Amt", "Tot_Mdcr_Alowd_Dscrd_Amt",
                           "PCT_Admnrd_Units", "PCT_Dscrd_Units"])
    year = whole_year(entry)
    for line_no, row in rows:
        code, brand, generic, allowed, administered, discarded, pct_admin, pct_disc = row
        yield line_no, {
            "data_year": year, "hcpcs_code": clean(code), "brand_name": clean(brand),
            "generic_name": clean(generic), "total_allowed_amount": decimal(allowed),
            "total_allowed_administered_amount": decimal(administered),
            "total_allowed_discarded_amount": decimal(discarded),
            "pct_administered_units": decimal(pct_admin), "pct_discarded_units": decimal(pct_disc),
        }


def read_geo(header, rows, entry):
    expect_header(header, ["Rndrng_Prvdr_Geo_Lvl", "Rndrng_Prvdr_Geo_Cd", "Rndrng_Prvdr_Geo_Desc",
                           "HCPCS_Cd", "HCPCS_Desc", "HCPCS_Drug_Ind", "Place_Of_Srvc",
                           "Tot_Rndrng_Prvdrs", "Tot_Benes", "Tot_Srvcs", "Tot_Bene_Day_Srvcs",
                           "Avg_Sbmtd_Chrg", "Avg_Mdcr_Alowd_Amt", "Avg_Mdcr_Pymt_Amt", "Avg_Mdcr_Stdzd_Amt"])
    year = whole_year(entry)
    for line_no, row in rows:
        (level, geo_code, geo_desc, code, desc, drug, pos, providers, benes, services, bene_days,
         charge, allowed, payment, standardized) = row
        yield line_no, {
            "data_year": year, "geo_level": clean(level), "geo_code": clean(geo_code),
            "geo_description": clean(geo_desc), "hcpcs_code": clean(code), "hcpcs_description": clean(desc),
            "hcpcs_drug_indicator": clean(drug), "place_of_service": clean(pos),
            "total_rendering_providers": integer(providers), "total_beneficiaries": integer(benes),
            "total_services": decimal(services), "total_beneficiary_day_services": decimal(bene_days),
            "avg_submitted_charge": decimal(charge), "avg_medicare_allowed_amount": decimal(allowed),
            "avg_medicare_payment_amount": decimal(payment),
            "avg_medicare_standardized_amount": decimal(standardized),
        }


def whole_year(entry: dict) -> int:
    if not re.fullmatch(r"20\d\d", entry["period"]):
        raise FormatError(f"expected a calendar-year file, but the catalog period is {entry['period']!r}")
    return int(entry["period"])


READERS = {
    "part_b_spending": (PART_B, read_part_b),
    "part_b_spending_quarterly": (PART_B_QUARTERLY, read_part_b_quarterly),
    "discarded_drug_units": (DISCARDED, read_discarded),
    "physician_geo_service": (GEO, read_geo),
}


# --------------------------------------------------------------------------
# Reading a file from raw/
# --------------------------------------------------------------------------

def read_bytes(entry: dict) -> bytes:
    """The file exactly as CMS served it, checked against the manifest hash."""
    data = (ROOT / entry["local_path"]).read_bytes()
    if entry["stored_as"] == "gzip":
        if hashlib.sha256(data).hexdigest() != entry["stored_sha256"]:
            raise FormatError("stored file's SHA-256 does not match the manifest")
        data = gzip.decompress(data)
    elif entry["stored_as"] != "csv":
        raise FormatError(f"unknown stored_as {entry['stored_as']!r}")
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise FormatError("SHA-256 does not match the manifest")
    return data


def decode(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "windows-1252"


# --------------------------------------------------------------------------
# Building the tables (parse.py calls prepare(), then create())
# --------------------------------------------------------------------------

@dataclass
class Prepared:
    counts: dict[str, int] = field(default_factory=lambda: {t: 0 for t in TABLES})
    source_rows: list[tuple] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def prepare(work: Path) -> Prepared:
    """Read every file in the manifest and write one CSV per table into work/."""
    out = Prepared()
    entries = []
    if MANIFEST_PATH.exists():
        entries = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["files"]
    newest: dict[str, int] = {}
    for e in entries:
        newest[e["key"]] = max(newest.get(e["key"], 0), e["version"])

    handles, writers = [], {}
    for t in TABLES.values():
        fh = (work / f"{t.name}.csv").open("w", encoding="utf-8", newline="")
        handles.append(fh)
        writers[t.name] = csv.DictWriter(fh, fieldnames=t.all_columns, restval="\\N")
        writers[t.name].writeheader()

    for e in sorted(entries, key=lambda e: (e["series"], e["period"], e["version"])):
        where = e["local_path"]
        if e["series"] not in READERS:
            out.problems.append(f"{where}: unknown series {e['series']!r}")
            continue
        table, reader = READERS[e["series"]]
        is_current = e["version"] == newest[e["key"]]
        try:
            text, encoding = decode(read_bytes(e))
            lines = csv.reader(io.StringIO(text))
            header = [clean(h) for h in next(lines)]
            rows = ((n, row) for n, row in enumerate(lines, start=2) if any(c.strip() for c in row))
            checked = (check_width(n, row, len(header)) for n, row in rows)
            n_rows = 0
            for line_no, values in reader(header, checked, e):
                writers[table.name].writerow({
                    **{k: ("\\N" if v is None else v) for k, v in values.items()},
                    "release_period": e["period"], "source_file": e["local_path"], "source_row": line_no,
                    "cms_posted_date": e["catalog_modified"], "file_version": e["version"],
                    "is_current": is_current, "is_latest_release": False,
                })
                n_rows += 1
        except (FormatError, ValueError, OSError) as err:
            out.problems.append(f"{where}: {err}")
            continue
        if n_rows == 0:
            out.problems.append(f"{where}: header found but no data rows")
            continue
        out.counts[table.name] += n_rows
        member = "(decompressed)" if e["stored_as"] == "gzip" else None
        out.source_rows.append((table.name, e["period"], e["local_path"], member, e["catalog_modified"],
                                e["version"], is_current, e["sha256"], encoding, 1, n_rows, 0))
    for fh in handles:
        fh.close()
    return out


def check_width(line_no: int, row: list[str], width: int) -> tuple[int, list[str]]:
    if len(row) != width:
        raise FormatError(f"line {line_no} has {len(row)} cells; the header has {width}")
    return line_no, row


def create(con, work: Path) -> None:
    """Create the tables from the CSVs prepare() wrote, then set is_latest_release."""
    for t in TABLES.values():
        cols = t.columns + SOURCE_COLUMNS
        con.execute(f"CREATE TABLE {t.name} ({', '.join(f'{c} {typ}' for c, typ in cols)})")
        casts = ", ".join(f"CAST({c} AS {typ})" for c, typ in cols)
        con.execute(
            f"INSERT INTO {t.name} SELECT {casts} FROM read_csv(?, header=true, all_varchar=true, "
            f"quote='\"', escape='\"', nullstr='\\N')",
            [str(work / f"{t.name}.csv")],
        )
        # The newest release that covers each period wins. Release labels
        # sort in time order within one dataset ("2024" < "2025", "2026Q1" < "2026Q2").
        con.execute(f"""
            UPDATE {t.name} AS t SET is_latest_release = t.is_current AND t.release_period = m.latest
            FROM (SELECT {t.period_column} AS period, max(release_period) AS latest
                  FROM {t.name} WHERE is_current GROUP BY 1) AS m
            WHERE t.{t.period_column} = m.period
        """)
