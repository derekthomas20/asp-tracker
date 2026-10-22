"""Save the outside sources that check data/companies.json.

Three free sources, each saved as a small extract under
reference/company_sources/<source>/<UTC date and time>_<first 12 of SHA-256>.<ext>
and recorded in reference/company_sources_manifest.json. A new snapshot is
written only when the extract changed. Snapshots are never changed or
deleted.

  fda_labelers  FDA NDC Directory (https://www.accessdata.fda.gov/cder/ndctext.zip):
                the company names FDA lists under each labeler code in
                companies.json. The full ~11 MB ZIP changes daily, so only
                the rows for those codes are kept (its SHA-256 is recorded).
  gleif         GLEIF API (https://api.gleif.org): for each of those FDA
                names, its Legal Entity Identifier and ultimate parent.
                About 3 requests per name at GLEIF's limit of 60 a minute,
                so this part takes several minutes.
  sec_tickers   SEC company tickers (https://www.sec.gov/files/company_tickers_exchange.json):
                the tickers for the CIKs and tickers in companies.json.
                SEC requires a contact email in every request: set the
                environment variable SEC_CONTACT_EMAIL (on GitHub, the
                repository variable of the same name).

A source that fails is skipped and the others are still saved; the script
then exits with an error so the problem is noticed.

Usage (from the repository root):
    $env:SEC_CONTACT_EMAIL = "you@example.com"
    .venv\\Scripts\\python scripts/fetch_company_sources.py
    .venv\\Scripts\\python scripts/fetch_company_sources.py --only sec_tickers
Then run scripts/companies_report.py.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

from companies import (ROOT, SOURCES_DIR, SOURCES_MANIFEST, load_companies, load_fda_labelers,
                       mapped_codes, normalize_name)

FDA_URL = "https://www.accessdata.fda.gov/cder/ndctext.zip"
GLEIF_API = "https://api.gleif.org/api/v1/lei-records"
SEC_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
USER_AGENT = "asp-tracker (public research site on Medicare Part B drug payment limits)"
GLEIF_PAUSE_SECONDS = 1.05  # GLEIF allows 60 requests a minute
TIMEOUT_SECONDS = 120

FDA_COLUMNS = {"PRODUCTNDC", "LABELERNAME"}


def get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return resp.read()


def save_snapshot(manifest: dict, source: str, url: str, data: bytes, ext: str, note: dict | None = None) -> None:
    sha256 = hashlib.sha256(data).hexdigest()
    entry = manifest["sources"].setdefault(source, {"url": url, "snapshots": []})
    if entry["snapshots"] and entry["snapshots"][-1]["sha256"] == sha256:
        print(f"  {source}: no change since {entry['snapshots'][-1]['local_path']}")
        return
    fetched = datetime.now(timezone.utc).replace(microsecond=0)
    folder = SOURCES_DIR / source
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{fetched:%Y-%m-%dT%H%M%SZ}_{sha256[:12]}.{ext}"
    if path.exists():
        raise SystemExit(f"STOPPED: {path.relative_to(ROOT)} already exists; not overwriting it.")
    path.write_bytes(data)
    entry["snapshots"].append({
        "local_path": path.relative_to(ROOT).as_posix(),
        "fetched_at": fetched.isoformat(),
        "sha256": sha256,
        "size_bytes": len(data),
        **(note or {}),
    })
    SOURCES_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"  {source}: saved {path.relative_to(ROOT)} ({len(data):,} bytes)")


def fetch_fda(doc: dict, manifest: dict) -> None:
    """Company names FDA lists under each labeler code in companies.json."""
    with tempfile.TemporaryFile() as tmp:
        tmp.write(get(FDA_URL))
        tmp.seek(0)
        zip_sha256 = hashlib.sha256(tmp.read()).hexdigest()
        tmp.seek(0)
        with zipfile.ZipFile(tmp) as z:
            raw = z.read("product.txt")
    # The file is not UTF-8 (it has Windows-1252 characters such as ®).
    text = raw.decode("cp1252")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    missing = FDA_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise RuntimeError(f"FDA product.txt has no {sorted(missing)} column; the format changed")
    wanted = set(mapped_codes(doc))
    names: set[tuple[str, str]] = set()
    for row in reader:
        segment = row["PRODUCTNDC"].split("-")[0]
        if not segment.isdigit() or len(segment) not in (4, 5):
            raise RuntimeError(f"FDA product NDC {row['PRODUCTNDC']!r} has an unrecognized labeler code")
        code = segment.zfill(5)  # FDA's 10-digit 4-4-2 NDCs are 0-padded to 5-4-2 in CMS files
        if code in wanted:
            names.add((code, row["LABELERNAME"].strip()))
    out = io.StringIO()
    out.write("labeler_code\tlabeler_name\n")
    for code, name in sorted(names):
        out.write(f"{code}\t{name}\n")
    print(f"  fda_labelers: {len({c for c, _ in names})} of {len(wanted)} codes have FDA listings")
    save_snapshot(manifest, "fda_labelers", FDA_URL, out.getvalue().encode("utf-8"), "tsv",
                  {"source_zip_sha256": zip_sha256})


def gleif_get(path: str) -> dict | None:
    time.sleep(GLEIF_PAUSE_SECONDS)
    try:
        return json.loads(get(path, {"Accept": "application/vnd.api+json"}))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


def gleif_lookup(name: str) -> dict:
    """Find the entity whose legal (or other) name equals this name, and its
    ultimate parent. A fuzzy GLEIF search result that is not an exact match
    is never used."""
    want = normalize_name(name)
    # FDA writes some apostrophes as ’ or ¿ ("Dr. Reddy¿s"); GLEIF rejects those.
    query_name = name.replace("’", "'").replace("¿", "'")
    query = urllib.parse.urlencode({"filter[entity.legalName]": query_name, "page[size]": 20})
    try:
        found = gleif_get(f"{GLEIF_API}?{query}") or {}
    except urllib.error.HTTPError as err:
        if err.code != 400:
            raise
        return {"name": name, "lei": None, "matches": 0, "error": "GLEIF rejected the search (HTTP 400)"}
    hits = []
    for rec in found.get("data", []):
        ent = rec["attributes"]["entity"]
        all_names = [ent["legalName"]["name"]] + [n["name"] for n in ent.get("otherNames", [])]
        if ent.get("status") == "ACTIVE" and any(normalize_name(n) == want for n in all_names):
            hits.append(rec)
    if len(hits) != 1:
        return {"name": name, "lei": None, "matches": len(hits)}
    rec = hits[0]
    lei = rec["id"]
    ent = rec["attributes"]["entity"]
    out = {"name": name, "lei": lei, "legal_name": ent["legalName"]["name"],
           "country": ent["legalAddress"]["country"], "parent_lei": None, "parent_name": None,
           "parent_exception": None}
    parent = gleif_get(f"{GLEIF_API}/{lei}/ultimate-parent")
    if parent and parent.get("data"):
        out["parent_lei"] = parent["data"]["id"]
        out["parent_name"] = parent["data"]["attributes"]["entity"]["legalName"]["name"]
    else:
        exc = gleif_get(f"{GLEIF_API}/{lei}/ultimate-parent-reporting-exception")
        if exc and exc.get("data"):
            out["parent_exception"] = exc["data"]["attributes"]["reason"]
    return out


def fetch_gleif(manifest: dict) -> None:
    """GLEIF record and ultimate parent for every name in the newest FDA extract."""
    fda = load_fda_labelers()
    if fda is None:
        raise RuntimeError("no FDA extract saved yet; run without --only first")
    names = sorted({n for ns in fda.values() for n in ns}, key=normalize_name)
    print(f"  gleif: looking up {len(names)} names (several minutes)")
    out = {}
    for name in names:
        key = normalize_name(name)
        if key not in out:
            out[key] = gleif_lookup(name)
    data = (json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    save_snapshot(manifest, "gleif", GLEIF_API, data, "json")


def fetch_sec(doc: dict, manifest: dict) -> None:
    """SEC's tickers for every CIK and US ticker in companies.json."""
    email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if "@" not in email:
        raise RuntimeError("SEC_CONTACT_EMAIL is not set; SEC refuses requests without a contact email")
    payload = json.loads(get(SEC_URL, {"User-Agent": f"asp-tracker {email}"}))
    if payload.get("fields") != ["cik", "name", "ticker", "exchange"]:
        raise RuntimeError(f"SEC ticker file has fields {payload.get('fields')}; the format changed")
    ciks = {c["sec_cik"] for c in doc["companies"] if c["sec_cik"]}
    tickers = {c["us_listing"]["ticker"] for c in doc["companies"] if c["us_listing"]}
    rows = [dict(zip(payload["fields"], r)) for r in payload["data"]]
    keep = sorted((r for r in rows if r["cik"] in ciks or r["ticker"] in tickers),
                  key=lambda r: (r["cik"], r["ticker"]))
    data = (json.dumps(keep, indent=1) + "\n").encode("utf-8")
    save_snapshot(manifest, "sec_tickers", SEC_URL, data, "json")


SOURCES = ("fda_labelers", "gleif", "sec_tickers")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=SOURCES, action="append", help="fetch just this source (repeatable)")
    args = parser.parse_args()

    doc = load_companies()
    manifest = {"sources": {}}
    if SOURCES_MANIFEST.exists():
        manifest = json.loads(SOURCES_MANIFEST.read_text(encoding="utf-8"))

    steps = {"fda_labelers": lambda: fetch_fda(doc, manifest),
             "gleif": lambda: fetch_gleif(manifest),
             "sec_tickers": lambda: fetch_sec(doc, manifest)}
    failed = []
    for source in args.only or SOURCES:
        print(f"{source}:")
        try:
            steps[source]()
        except (RuntimeError, OSError, ValueError, KeyError, zipfile.BadZipFile) as err:
            print(f"  FAILED: {err}")
            failed.append(source)
    if failed:
        print(f"Failed: {', '.join(failed)}. The other sources were saved.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
