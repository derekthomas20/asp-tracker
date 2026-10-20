"""Load the CMS files in raw/ into data/asp.duckdb.

Reads every ZIP listed in data/manifest.json (written by scrape.py) and
builds these tables:

    payment_limits   one long table: every row of every payment limit /
                     ASP pricing file, for every quarter and every version
    noc_limits       Not Otherwise Classified (NOC) drug payment limits
    crosswalks       NDC-HCPCS crosswalks (ASP, NOC, OPPS, AWP, COVID-19)
    source_files     one row per CSV loaded: where it came from, its
                     encoding, header row, and how many rows were read
    biosimilar_groups  the site owner's biosimilar/reference product
                     groupings, from the newest snapshot saved by
                     scripts/fetch_groups.py (not a CMS file; no prices)

It also loads the Medicare usage files listed in data/cms_data_manifest.json
(written by scripts/fetch_cms_data.py); see scripts/cms_data.py for those
tables (part_b_spending, part_b_spending_quarterly, discarded_drug_units,
physician_geo_service).

When CMS has posted more than one version of a file for the same quarter,
every version is loaded and the newest one has is_current = true.

The database is rebuilt from scratch on every run, so running this twice
gives the same result and never creates duplicate rows. Files in raw/ are
only read, never changed.

Usage (from the repository root):
    .venv\\Scripts\\python scripts/parse.py
    .venv\\Scripts\\python scripts/parse.py --groups-file path\\to\\export.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb

import cms_data

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "data" / "manifest.json"
DB_PATH = ROOT / "data" / "asp.duckdb"

MONTH_TO_QUARTER = {"january": 1, "april": 2, "july": 3, "october": 4}

# Columns stored with every row so it can be traced back to its source.
SOURCE_COLUMNS = ["source_file", "source_member", "source_row", "cms_posted_date",
                  "file_version", "is_current"]

# --------------------------------------------------------------------------
# Column names: every header CMS has used, mapped to one name of ours.
# Headers are compared after lower-casing and squeezing spaces. A header that
# is not listed here stops the load, so a new CMS format can't slip through.
# --------------------------------------------------------------------------

PAYMENT_COLUMNS = {
    "hcpcs code": "hcpcs_code",
    "hcpcs": "hcpcs_code",
    "short description": "short_description",
    "short descriptor": "short_description",
    "hcpcs code dosage": "hcpcs_dosage",
    "hcpcs dosage": "hcpcs_dosage",
    "payment limit": "payment_limit",
    "co-insurance percentage": "coinsurance_percentage",
    "coinsurance percentage": "coinsurance_percentage",
    "vaccine awp%": "vaccine_awp_pct",
    "vaccine limit": "vaccine_limit",
    "blood awp%": "blood_awp_pct",
    "blood limit": "blood_limit",
    "clotting factor": "clotting_factor",
    "notes": "notes",
}

NOC_COLUMNS = {
    "drug generic name (trade name)": "drug_generic_name",
    "drug generic name": "drug_generic_name",
    "dosage": "dosage",
    "payment limit": "payment_limit",
    "notes": "notes",
}

CROSSWALK_COLUMNS = {
    "hcpcs code": "hcpcs_code",
    "hcpcs": "hcpcs_code",
    # "_2026_CODE" style headers are handled in map_header()
    "short description": "short_description",
    "short descriptor": "short_description",
    "drug generic name": "drug_generic_name",
    "labeler name": "labeler_name",
    "ndc": "ndc",
    "ndc2": "ndc",
    "ndc or alternate id": "ndc",
    "drug name": "drug_name",
    "drug/prodecure name": "drug_name",  # CMS spelling
    "drug/procedure name": "drug_name",
    "hcpcs dosage": "hcpcs_dosage",
    "dosage": "hcpcs_dosage",
    "pkg size": "pkg_size",
    "pkg qty": "pkg_qty",
    "billunits": "bill_units",
    "billunitspkg": "bill_units_pkg",
}


@dataclass
class TableSpec:
    name: str
    columns: dict[str, str]  # header alias -> our column name
    key_column: str  # a row without this value is not a data row
    output: list[str]  # our columns, in table order (before source columns)
    decimal_columns: tuple[str, ...] = ()


PAYMENT = TableSpec(
    "payment_limits", PAYMENT_COLUMNS, "hcpcs_code",
    ["effective_quarter", "hcpcs_code", "short_description", "hcpcs_dosage",
     "payment_limit", "payment_limit_text", "coinsurance_percentage",
     "vaccine_awp_pct", "vaccine_limit", "blood_awp_pct", "blood_limit",
     "clotting_factor", "notes"],
    decimal_columns=("payment_limit",),
)
NOC = TableSpec(
    "noc_limits", NOC_COLUMNS, "drug_generic_name",
    ["effective_quarter", "drug_generic_name", "dosage", "payment_limit",
     "payment_limit_text", "notes"],
    decimal_columns=("payment_limit",),
)
CROSSWALK = TableSpec(
    "crosswalks", CROSSWALK_COLUMNS, "ndc",
    ["effective_quarter", "crosswalk_type", "hcpcs_code", "short_description",
     "drug_generic_name", "labeler_name", "ndc", "drug_name", "hcpcs_dosage",
     "pkg_size", "pkg_qty", "bill_units", "bill_units_pkg"],
)


# --------------------------------------------------------------------------
# Reading one CSV
# --------------------------------------------------------------------------

class FormatError(Exception):
    """The file doesn't look like anything we know how to read."""


def clean(cell: str) -> str:
    """Turn non-breaking spaces into normal spaces and trim."""
    return " ".join(cell.replace("\xa0", " ").split())


def decode(data: bytes) -> tuple[str, str]:
    """CMS files are a mix of UTF-8 and Windows-1252. Try UTF-8 first."""
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "windows-1252"


def map_header(cell: str, spec: TableSpec) -> str | None:
    key = clean(cell).lower()
    if not key:
        return None
    if spec is CROSSWALK and re.fullmatch(r"_\d{4}_code", key):
        return "hcpcs_code"
    if key in spec.columns:
        return spec.columns[key]
    raise FormatError(f"unknown column header {cell!r}")


def find_header(rows: list[list[str]], spec: TableSpec) -> int:
    """Index of the header row: the first row where one cell is exactly a
    known name for the key column (e.g. 'HCPCS Code'). The notes above the
    header also mention HCPCS, so a plain text search is not enough."""
    key_aliases = {alias for alias, col in spec.columns.items() if col == spec.key_column}
    for i, row in enumerate(rows):
        for cell in row:
            c = clean(cell).lower()
            if c in key_aliases or (spec is CROSSWALK and re.fullmatch(r"_\d{4}_code", c)):
                return i
    raise FormatError(f"no header row found (looked for {sorted(key_aliases)})")


def stated_quarter(rows: list[list[str]]) -> str | None:
    """The quarter the file says it covers, from a line like
    'Effective January 1, 2021 through March 31, 2021'."""
    for row in rows:
        text = clean(" ".join(row))
        m = re.search(r"\b(January|April|July|October)\s+1,\s*(20\d\d)\b", text, re.IGNORECASE)
        if m:
            return f"{m.group(2)}Q{MONTH_TO_QUARTER[m.group(1).lower()]}"
    return None


def parse_decimal(text: str) -> str | None:
    """'$1,234.567' -> '1234.567'. Returns None if blank or not a number.
    Kept as text so no rounding happens before DuckDB stores it exactly."""
    t = text.replace("$", "").replace(",", "").strip()
    if not t:
        return None
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    if d.as_tuple().exponent < -6:
        raise FormatError(f"more decimal places than the table allows: {text!r}")
    return str(d)


@dataclass
class ParsedFile:
    rows: list[dict]
    encoding: str
    header_row: int  # 1-based line number of the header
    stated_quarter: str | None
    odd_rows: list[tuple[int, list[str]]] = field(default_factory=list)


def parse_csv(data: bytes, spec: TableSpec) -> ParsedFile:
    text, encoding = decode(data)
    rows = list(csv.reader(io.StringIO(text)))
    h = find_header(rows, spec)
    header = [map_header(c, spec) for c in rows[h]]

    out, odd = [], []
    for line_no, row in enumerate(rows[h + 1:], start=h + 2):
        values: dict[str, str] = {}
        stray = []
        for col, cell in zip(header, row):
            v = clean(cell)
            if col is None:
                if v:
                    stray.append(v)
            else:
                values[col] = v
        stray += [clean(c) for c in row[len(header):] if clean(c)]
        if not any(values.values()) and not stray:
            continue  # blank line
        if not values.get(spec.key_column) or stray:
            odd.append((line_no, row))  # footnote or something unexpected
            continue
        for col in spec.decimal_columns:
            raw = values.get(col, "")
            values[f"{col}_text"] = raw
            values[col] = parse_decimal(raw)
        values["source_row"] = line_no
        out.append(values)
    return ParsedFile(out, encoding, h + 1, stated_quarter(rows[:h]), odd)


# --------------------------------------------------------------------------
# Choosing which CSV inside each ZIP to read
# --------------------------------------------------------------------------

def crosswalk_type(member: str) -> str:
    name = member.lower()
    for label, value in (("covid", "COVID-19"), ("prep", "PrEP"), ("opps", "OPPS"), ("awp", "AWP"),
                         ("noc", "NOC"), ("asp", "ASP")):
        if label in name:
            return value
    raise FormatError(f"can't tell crosswalk type from {member!r}")


def members_to_load(zf: zipfile.ZipFile, category: str) -> tuple[list[tuple[str, TableSpec]], list[str]]:
    """Returns ([(csv name, table)], [skipped file names with reason])."""
    load, skipped = [], []
    for member in zf.namelist():
        base = member.rsplit("/", 1)[-1]
        lower = base.lower()
        if member.startswith("__MACOSX/") or not lower.endswith(".csv"):
            continue  # Excel copies and Mac metadata; we read the CSV versions
        if "introduction text" in lower:
            skipped.append(f"{base} (crosswalk introduction text)")
        elif "not payable" in lower:
            skipped.append(f"{base} (drugs not payable list; not loaded yet)")
        elif category == "payment_limit" and ("pricing file" in lower or "payment limit file" in lower):
            load.append((member, PAYMENT))
        elif category == "noc" and "noc" in lower:
            load.append((member, NOC))
        elif category == "crosswalk" and "crosswalk" in lower:
            load.append((member, CROSSWALK))
        else:
            skipped.append(f"{base} (not recognised)")
    if category == "payment_limit" and len(load) != 1:
        raise FormatError(f"expected 1 payment limit CSV, found {len(load)}: {zf.namelist()}")
    return load, skipped


# --------------------------------------------------------------------------
# Building the database
# --------------------------------------------------------------------------

SQL_TYPES = {
    "payment_limit": "DECIMAL(18,6)",
    "source_row": "INTEGER",
    "cms_posted_date": "DATE",
    "file_version": "INTEGER",
    "is_current": "BOOLEAN",
}


def create_table(con, spec: TableSpec, csv_path: Path) -> None:
    cols = spec.output + SOURCE_COLUMNS
    defs = ", ".join(f"{c} {SQL_TYPES.get(c, 'VARCHAR')}" for c in cols)
    con.execute(f"CREATE TABLE {spec.name} ({defs})")
    casts = ", ".join(f"CAST({c} AS {SQL_TYPES.get(c, 'VARCHAR')})" for c in cols)
    con.execute(
        f"INSERT INTO {spec.name} SELECT {casts} FROM read_csv(?, header=true, "
        f"all_varchar=true, quote='\"', escape='\"', nullstr='\\N')",
        [str(csv_path)],
    )


GROUPS_MANIFEST = ROOT / "reference" / "manifest.json"
GROUP_COLUMNS = ["molecule", "hcpcs", "role", "brand_names", "cms_description", "qualifies_8pct",
                 "valid_from", "valid_to", "verified", "source", "notes"]


def read_groups(override: str | None) -> tuple[str | None, list[tuple]]:
    """Read the newest biosimilar_groups snapshot (or the file given with
    --groups-file). Returns (file name, rows). Stops on anything unexpected."""
    if override:
        path = Path(override)
    elif GROUPS_MANIFEST.exists():
        snapshots = json.loads(GROUPS_MANIFEST.read_text(encoding="utf-8"))["snapshots"]
        if not snapshots:
            return None, []
        path = ROOT / snapshots[-1]["local_path"]
    else:
        return None, []
    name = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)

    rows = list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig"))))
    if not rows or [clean(c).lower() for c in rows[0]] != GROUP_COLUMNS:
        raise FormatError(f"{name}: columns are not {GROUP_COLUMNS}")
    out, problems = [], []
    for line_no, row in enumerate(rows[1:], start=2):
        row = [clean(c) for c in row] + [""] * (len(GROUP_COLUMNS) - len(row))
        if not any(row):
            continue
        v = dict(zip(GROUP_COLUMNS, row))
        if not re.fullmatch(r"[A-Z0-9]{5}", v["hcpcs"]):
            problems.append(f"line {line_no}: hcpcs {v['hcpcs']!r} is not a 5-character code")
        if v["role"].lower() not in ("reference", "biosimilar"):
            problems.append(f"line {line_no}: role {v['role']!r} is not 'reference' or 'biosimilar'")
        if v["verified"].lower() not in ("yes", "no", ""):
            problems.append(f"line {line_no}: verified {v['verified']!r} is not 'yes' or 'no'")
        if not v["molecule"]:
            problems.append(f"line {line_no}: molecule is blank")
        out.append((v["molecule"], v["hcpcs"], v["role"].lower(), v["brand_names"],
                    v["cms_description"], v["qualifies_8pct"], v["valid_from"], v["valid_to"],
                    v["verified"].lower() == "yes", v["source"], v["notes"], name, line_no))
    if problems:
        raise FormatError(f"{name}:\n    " + "\n    ".join(problems))
    return name, out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--groups-file", help="load this biosimilar groups CSV instead of the newest "
                        "snapshot (for checking a sheet export before fetching it)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    with MANIFEST_PATH.open(encoding="utf-8") as f:
        manifest = json.load(f)

    entries = manifest["files"]
    newest = {}
    for e in entries:
        newest[e["key"]] = max(newest.get(e["key"], 0), e["version"])

    work = Path(tempfile.mkdtemp(prefix="asp-parse-"))
    writers, handles = {}, []
    for spec in (PAYMENT, NOC, CROSSWALK):
        fh = (work / f"{spec.name}.csv").open("w", encoding="utf-8", newline="")
        handles.append(fh)
        w = csv.DictWriter(fh, fieldnames=spec.output + SOURCE_COLUMNS, restval="\\N")
        w.writeheader()
        writers[spec.name] = w

    source_rows, skipped_all, problems = [], [], []
    counts = {PAYMENT.name: 0, NOC.name: 0, CROSSWALK.name: 0}

    for e in sorted(entries, key=lambda e: (e["effective_period"], e["category"], e["version"])):
        if e["category"] not in ("payment_limit", "noc", "crosswalk"):
            skipped_all.append(f"{e['local_path']} ({e['category']} file; not loaded yet)")
            continue
        is_current = e["version"] == newest[e["key"]]
        with zipfile.ZipFile(ROOT / e["local_path"]) as zf:
            load, skipped = members_to_load(zf, e["category"])
            skipped_all += [f"{e['local_path']} -> {s}" for s in skipped]
            for member, spec in load:
                where = f"{e['local_path']} -> {member}"
                try:
                    parsed = parse_csv(zf.read(member), spec)
                    extra = {"crosswalk_type": crosswalk_type(member)} if spec is CROSSWALK else {}
                except FormatError as err:
                    problems.append(f"{where}: {err}")
                    continue
                if parsed.stated_quarter and parsed.stated_quarter != e["effective_period"]:
                    problems.append(f"{where}: file says {parsed.stated_quarter} but the "
                                    f"CMS page lists it as {e['effective_period']}")
                    continue
                if not parsed.rows:
                    problems.append(f"{where}: header found but no data rows")
                    continue
                for line_no, row in parsed.odd_rows:
                    text = " | ".join(clean(c) for c in row if clean(c))
                    skipped_all.append(f"{where} line {line_no}: not a data row: {text[:120]}")

                for r in parsed.rows:
                    writers[spec.name].writerow({
                        **{k: ("\\N" if v is None else v) for k, v in r.items()},
                        **extra,
                        "effective_quarter": e["effective_period"],
                        "source_file": e["local_path"],
                        "source_member": member,
                        "cms_posted_date": e.get("posted_date") or "\\N",
                        "file_version": e["version"],
                        "is_current": is_current,
                    })
                counts[spec.name] += len(parsed.rows)
                source_rows.append((spec.name, e["effective_period"], e["local_path"], member,
                                    e.get("posted_date"), e["version"], is_current, e["sha256"],
                                    parsed.encoding, parsed.header_row, len(parsed.rows),
                                    len(parsed.odd_rows)))

    for fh in handles:
        fh.close()

    usage = cms_data.prepare(work)
    problems += usage.problems
    source_rows += usage.source_rows

    if problems:
        print("STOPPED: these files could not be read. Nothing was written.\n")
        for p in problems:
            print(f"  {p}")
        return 1

    tmp_db = DB_PATH.with_suffix(".duckdb.tmp")
    tmp_db.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp_db))
    for spec in (PAYMENT, NOC, CROSSWALK):
        create_table(con, spec, work / f"{spec.name}.csv")
    cms_data.create(con, work)
    con.execute("""
        CREATE TABLE source_files (
            table_name VARCHAR, effective_quarter VARCHAR, source_file VARCHAR,
            source_member VARCHAR, cms_posted_date DATE, file_version INTEGER,
            is_current BOOLEAN, sha256 VARCHAR, encoding VARCHAR,
            header_row INTEGER, rows_loaded INTEGER, rows_skipped INTEGER)
    """)
    con.executemany("INSERT INTO source_files VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", source_rows)

    try:
        groups_file, group_rows = read_groups(args.groups_file)
    except FormatError as err:
        con.close()
        tmp_db.unlink(missing_ok=True)
        print(f"STOPPED: biosimilar groups file could not be read. Nothing was written.\n  {err}")
        return 1
    con.execute("""
        CREATE TABLE biosimilar_groups (
            molecule VARCHAR, hcpcs_code VARCHAR, role VARCHAR, brand_names VARCHAR,
            cms_description VARCHAR, qualifies_8pct VARCHAR, valid_from VARCHAR,
            valid_to VARCHAR, verified BOOLEAN, source VARCHAR, notes VARCHAR,
            source_file VARCHAR, source_row INTEGER)
    """)
    if group_rows:
        con.executemany("INSERT INTO biosimilar_groups VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", group_rows)

    # Sanity checks: every number must have come through intact.
    lost = con.execute("""
        SELECT count(*) FROM payment_limits
        WHERE payment_limit IS NULL AND payment_limit_text <> ''
    """).fetchone()[0]
    dupes = con.execute("""
        SELECT count(*) FROM (
            SELECT effective_quarter, hcpcs_code, file_version FROM payment_limits
            GROUP BY ALL HAVING count(*) > 1)
    """).fetchone()[0]
    con.close()
    os.replace(tmp_db, DB_PATH)
    for p in work.iterdir():
        p.unlink()
    work.rmdir()

    print(f"Built {DB_PATH.relative_to(ROOT)}")
    for name, n in {**counts, **usage.counts}.items():
        print(f"  {name:<26} {n:>10,} rows")
    print(f"  {'source_files':<26} {len(source_rows):>10,} CSV files")
    if groups_file:
        n_verified = sum(1 for r in group_rows if r[8])
        print(f"  biosimilar_groups {len(group_rows):>7,} rows ({n_verified} verified) from {groups_file}")
    else:
        print("  biosimilar_groups       0 rows (no snapshot yet; run scripts/fetch_groups.py)")
    if lost:
        print(f"\nNOTE: {lost} payment limit values were not numbers; the original text is "
              f"kept in payment_limit_text.")
    if dupes:
        print(f"\nNOTE: {dupes} HCPCS codes appear more than once in the same file version.")
    if skipped_all:
        print(f"\nNot loaded ({len(skipped_all)}):")
        for s in skipped_all:
            print(f"  {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
