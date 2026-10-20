"""Download Medicare usage files from data.cms.gov and keep every version.

The ASP pricing files (scripts/scrape.py) say what Medicare pays per unit.
These files say how much is used. They all come from CMS's data catalog,
https://data.cms.gov/data.json, which lists every dataset with its CSV link
and the date CMS last changed it:

    part_b_spending            Medicare Part B Spending by Drug (yearly;
                               each file holds the last five years)
    part_b_spending_quarterly  Medicare Quarterly Part B Spending by Drug
    discarded_drug_units       Medicare Part B Discarded Drug Units
    physician_geo_service      Medicare Physician & Other Practitioners -
                               by Geography and Service (national and state
                               totals for every HCPCS code; 2021 onward)

Files are saved under raw/data-cms-gov/<dataset>/<period>/ with the date CMS
changed them in the file name, and recorded in data/cms_data_manifest.json
with their SHA-256 hash. A file CMS revises is saved as a new file next to
the old one; nothing in raw/ is ever overwritten or deleted.

The Geography and Service files are about 40 MB each, so they are stored
gzip-compressed (about 13 MB). Compression is lossless: parse.py
decompresses them and checks the result against the SHA-256 of the file as
CMS served it, which is the "sha256" recorded in the manifest.

Usage (from the repository root):
    python scripts/fetch_cms_data.py --dry-run   # show what would be downloaded
    python scripts/fetch_cms_data.py             # download and update the manifest

Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

CATALOG_URL = "https://data.cms.gov/data.json"
USER_AGENT = (
    "asp-tracker/0.1 (public research site on Medicare Part B drug payment "
    "limits; polite scraper, one request at a time)"
)
PAUSE_SECONDS = 3.0
TIMEOUT_SECONDS = 300

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw" / "data-cms-gov"
MANIFEST_PATH = ROOT / "data" / "cms_data_manifest.json"


@dataclass(frozen=True)
class Series:
    id: str  # our name, also the table name in asp.duckdb
    title: str  # dataset title in the catalog, before " : <date>"
    folder: str  # folder under raw/data-cms-gov/
    first_year: int | None = None  # skip periods that start before this year
    compress: bool = False  # store gzip-compressed


SERIES = [
    Series("part_b_spending", "Medicare Part B Spending by Drug", "part-b-spending-by-drug"),
    Series("part_b_spending_quarterly", "Medicare Quarterly Part B Spending by Drug",
           "quarterly-part-b-spending-by-drug"),
    Series("discarded_drug_units", "Medicare Part B Discarded Drug Units", "discarded-drug-units"),
    Series("physician_geo_service", "Medicare Physician & Other Practitioners - by Geography and Service",
           "physician-by-geography-and-service", first_year=2021, compress=True),
]


# --------------------------------------------------------------------------
# Reading the catalog
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    series: Series
    key: str  # "<series id>|<period>"
    period: str  # "2024", "2026Q1", or "2023-01-01_2023-06-30"
    period_start: str
    period_end: str
    url: str
    modified: str  # date CMS last changed the file, YYYY-MM-DD
    dataset_title: str
    landing_page: str | None
    reason: str = ""
    previous: dict | None = None


def fetch_catalog() -> dict:
    req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def period_label(start: str, end: str) -> str:
    """'2024-01-01'..'2024-12-31' -> '2024'; '2026-01-01'..'2026-03-31' -> '2026Q1'."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if s.year == e.year and (s.month, s.day, e.month, e.day) == (1, 1, 12, 31):
        return str(s.year)
    quarter_ends = {1: (3, 31), 4: (6, 30), 7: (9, 30), 10: (12, 31)}
    if s.year == e.year and s.day == 1 and quarter_ends.get(s.month) == (e.month, e.day):
        return f"{s.year}Q{(s.month - 1) // 3 + 1}"
    return f"{start}_{end}"


def find_files(catalog: dict) -> tuple[list[Candidate], list[str]]:
    """Every CSV in the catalog that belongs to one of SERIES.
    Returns (candidates, problems)."""
    found: dict[tuple[str, str], Candidate] = {}
    problems = []
    for ds in catalog["dataset"]:
        title = ds.get("title", "")
        series = next((s for s in SERIES if title.split(" : ")[0].strip() == s.title), None)
        if series is None:
            continue
        for dist in ds.get("distribution", []):
            if dist.get("mediaType") != "text/csv" or not dist.get("downloadURL"):
                continue
            temporal = dist.get("temporal") or ds.get("temporal") or []
            modified = dist.get("modified") or ds.get("modified")
            if len(temporal) != 1 or not modified:
                problems.append(f"{title}: CSV has no single time period or no modified date: {dist['downloadURL']}")
                continue
            start, end = temporal[0]["startDate"], temporal[0]["endDate"]
            if series.first_year and int(start[:4]) < series.first_year:
                continue
            period = period_label(start, end)
            cand = Candidate(series, f"{series.id}|{period}", period, start, end, dist["downloadURL"],
                             modified, title, (ds.get("landingPage") or {}).get("accessURL"))
            # CMS sometimes lists the same CSV under two catalog entries;
            # keep the one changed most recently.
            seen = found.get((cand.key, cand.url))
            if seen is None or cand.modified > seen.modified:
                found[(cand.key, cand.url)] = cand

    # Two different CSVs for the same dataset and period: keep both apart.
    by_key: dict[str, list[Candidate]] = {}
    for cand in found.values():
        by_key.setdefault(cand.key, []).append(cand)
    out = []
    for key, cands in by_key.items():
        if len(cands) > 1:
            for cand in cands:
                cand.key = f"{key}|{file_name_from_url(cand.url)}"
        out += cands
    return sorted(out, key=lambda c: c.key), problems


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        with MANIFEST_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    return {"source_catalog": CATALOG_URL, "files": []}


def save_manifest(manifest: dict) -> None:
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, MANIFEST_PATH)


def latest_versions(manifest: dict) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for entry in manifest["files"]:
        if entry["key"] not in latest or entry["version"] > latest[entry["key"]]["version"]:
            latest[entry["key"]] = entry
    return latest


def plan(cands: list[Candidate], manifest: dict) -> tuple[list[Candidate], list[Candidate]]:
    """Split into (to download, unchanged)."""
    latest = latest_versions(manifest)
    get, same = [], []
    for cand in cands:
        prev = latest.get(cand.key)
        cand.previous = prev
        if prev is None:
            cand.reason = "new"
        else:
            changes = []
            if prev["url"] != cand.url:
                changes.append("URL changed")
            if prev["catalog_modified"] != cand.modified:
                changes.append(f"modified {prev['catalog_modified']} -> {cand.modified}")
            if not changes:
                same.append(cand)
                continue
            cand.reason = "; ".join(changes)
        get.append(cand)
    return get, same


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------

def file_name_from_url(url: str) -> str:
    """'.../DSD_PTB_RY26_P06_V10_DYT24_HCPCS-%20260602.csv' -> 'DSD_PTB_RY26_P06_V10_DYT24_HCPCS-260602.csv'"""
    name = urllib.parse.unquote(urllib.parse.urlparse(url).path).rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "", name.replace(" ", ""))
    return name or "file.csv"


def download(url: str) -> tuple[Path, str, int, dict]:
    """Download to a temporary file. Returns (path, sha256, size, headers)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    sha, size = hashlib.sha256(), 0
    fd, tmp_name = tempfile.mkstemp(suffix=".csv.part")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp, os.fdopen(fd, "wb") as out:
            headers = {"etag": resp.headers.get("ETag"), "last_modified": resp.headers.get("Last-Modified")}
            while chunk := resp.read(1 << 16):
                sha.update(chunk)
                out.write(chunk)
                size += len(chunk)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return Path(tmp_name), sha.hexdigest(), size, headers


def destination(cand: Candidate) -> Path:
    """raw/data-cms-gov/<dataset>/<period>/<modified>_<CMS file name>[.gz], never an existing file."""
    base = f"{cand.modified}_{file_name_from_url(cand.url)}" + (".gz" if cand.series.compress else "")
    folder = RAW_DIR / cand.series.folder / cand.period
    candidate, n = folder / base, 2
    while candidate.exists():
        stem, suffix = base.split(".", 1)
        candidate = folder / f"{stem}_v{n}.{suffix}"
        n += 1
    return candidate


def store(tmp: Path, dest: Path, compress: bool) -> tuple[str, int]:
    """Move or compress the download into raw/. Returns (sha256, size) of the stored file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with tmp.open("rb") as src, part.open("xb") as out:
        if compress:
            # mtime=0 and no file name inside, so the same CSV always gives the same bytes.
            with gzip.GzipFile(filename="", mode="wb", fileobj=out, compresslevel=9, mtime=0) as gz:
                shutil.copyfileobj(src, gz, 1 << 20)
        else:
            shutil.copyfileobj(src, out, 1 << 20)
    tmp.unlink()
    os.rename(part, dest)  # fails rather than overwrite if dest exists
    sha, size = hashlib.sha256(), 0
    with dest.open("rb") as f:
        while chunk := f.read(1 << 20):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size


def fetch(cand: Candidate, manifest: dict) -> str:
    """Download one file and record it. Returns 'saved' or 'same-hash'."""
    tmp, sha, size, headers = download(cand.url)
    prev = cand.previous
    if prev and prev["sha256"] == sha:
        # Same bytes: only the catalog entry changed. Record that so the
        # next run doesn't download it again.
        tmp.unlink()
        prev.update({"url": cand.url, "catalog_modified": cand.modified, "dataset_title": cand.dataset_title})
        prev.update({k: v for k, v in headers.items() if v})
        return "same-hash"

    dest = destination(cand)
    stored_sha, stored_size = store(tmp, dest, cand.series.compress)
    manifest["files"].append({
        "key": cand.key,
        "version": (prev["version"] + 1) if prev else 1,
        "series": cand.series.id,
        "dataset_title": cand.dataset_title,
        "period": cand.period,
        "period_start": cand.period_start,
        "period_end": cand.period_end,
        "url": cand.url,
        "landing_page": cand.landing_page,
        "catalog_modified": cand.modified,
        "local_path": dest.relative_to(ROOT).as_posix(),
        "stored_as": "gzip" if cand.series.compress else "csv",
        "sha256": sha,
        "size_bytes": size,
        "stored_sha256": stored_sha,
        "stored_size_bytes": stored_size,
        "downloaded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "change_reason": cand.reason,
        **headers,
    })
    return "saved"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="show what would be downloaded; change nothing")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"Reading {CATALOG_URL}")
    cands, errors = find_files(fetch_catalog())
    for s in SERIES:
        if not any(c.series is s for c in cands):
            errors.append(f"no CSV files found in the catalog for {s.title!r}")
    manifest = load_manifest()
    to_get, unchanged = plan(cands, manifest)

    verb = "Would download" if args.dry_run else "Downloading"
    print(f"\n{verb} {len(to_get)} file(s); {len(unchanged)} unchanged since last run.")
    saved = 0
    for i, cand in enumerate(to_get, 1):
        print(f"  [{i}/{len(to_get)}] {cand.key:<38} modified {cand.modified}  ({cand.reason})\n      {cand.url}")
        if args.dry_run:
            continue
        try:
            result = fetch(cand, manifest)
            save_manifest(manifest)  # after each file, so a crash loses nothing
            if result == "saved":
                saved += 1
                print(f"      saved {manifest['files'][-1]['local_path']}")
            else:
                print("      same bytes as the file already saved; manifest updated")
        except (urllib.error.URLError, OSError) as e:
            errors.append(f"download failed for {cand.url}: {e}")
        if i < len(to_get):
            time.sleep(PAUSE_SECONDS)

    if args.dry_run:
        print("\nDry run: nothing was downloaded and the manifest was not changed.")
    else:
        print(f"\nSaved {saved} new file(s).")
    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for e in errors:
            print(f"  {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
