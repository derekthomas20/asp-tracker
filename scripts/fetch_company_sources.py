"""Save the outside sources that check data/companies.json.

Four extracts from three free sources, each saved as a small extract under
reference/company_sources/<source>/<UTC date and time>_<first 12 of SHA-256>.<ext>
and recorded in reference/company_sources_manifest.json. A new snapshot is
written only when the extract changed. Snapshots are never changed or
deleted.

  fda_labelers  FDA NDC Directory (https://www.accessdata.fda.gov/cder/ndctext.zip):
                the company names FDA lists under each labeler code in
                companies.json. The full ~11 MB ZIP changes daily, so only
                the rows for those codes are kept (its SHA-256 is recorded).
  fda_packages  The same FDA ZIP's package.txt: the 10-digit NDC FDA lists for
                each NDC in the newest CMS NDC-HCPCS crosswalk (CMS writes
                NDCs in the 11-digit 5-4-2 form, and the 10-digit form can't
                be worked out from it), with FDA's package description.
                Read from the crosswalk ZIP in raw/, so run scrape.py first.
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
FDA_PACKAGE_COLUMNS = {"NDCPACKAGECODE", "PACKAGEDESCRIPTION", "STARTMARKETINGDATE", "ENDMARKETINGDATE"}
ASP_MANIFEST = ROOT / "data" / "manifest.json"


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


_fda_zip: dict[str, tuple[str, dict[str, bytes]]] = {}


def fda_files() -> tuple[str, dict[str, bytes]]:
    """(SHA-256 of the FDA NDC Directory ZIP, {member: bytes}), downloaded
    once per run and shared by fda_labelers and fda_packages."""
    if not _fda_zip:
        with tempfile.TemporaryFile() as tmp:
            tmp.write(get(FDA_URL))
            tmp.seek(0)
            zip_sha256 = hashlib.sha256(tmp.read()).hexdigest()
            tmp.seek(0)
            with zipfile.ZipFile(tmp) as z:
                _fda_zip["zip"] = (zip_sha256, {m: z.read(m) for m in ("product.txt", "package.txt")})
    return _fda_zip["zip"]


def fda_reader(member: str, columns: set[str]) -> csv.DictReader:
    # The files are not UTF-8 (they have Windows-1252 characters such as ®).
    text = fda_files()[1][member].decode("cp1252")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    missing = columns - set(reader.fieldnames or [])
    if missing:
        raise RuntimeError(f"FDA {member} has no {sorted(missing)} column; the format changed")
    return reader


def fetch_fda(doc: dict, manifest: dict) -> None:
    """Company names FDA lists under each labeler code in companies.json."""
    zip_sha256 = fda_files()[0]
    reader = fda_reader("product.txt", FDA_COLUMNS)
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


def crosswalk_ndcs() -> tuple[str, set[str]]:
    """(local path, NDCs) of the newest version of the newest quarter's CMS
    NDC-HCPCS crosswalk in raw/, read the same way parse.py reads it."""
    import parse  # scripts/parse.py; imported here so the other sources don't need it
    files = json.loads(ASP_MANIFEST.read_text(encoding="utf-8"))["files"]
    crosswalks = [e for e in files if e["category"] == "crosswalk"]
    if not crosswalks:
        raise RuntimeError("no crosswalk in data/manifest.json; run scrape.py first")
    newest = max(crosswalks, key=lambda e: (e["effective_period"], e["version"]))
    ndcs = set()
    with zipfile.ZipFile(ROOT / newest["local_path"]) as zf:
        load, _ = parse.members_to_load(zf, "crosswalk")
        for member, spec in load:
            try:
                ndcs |= {r["ndc"] for r in parse.parse_csv(zf.read(member), spec).rows}
            except parse.FormatError as err:
                raise RuntimeError(f"{newest['local_path']} -> {member}: {err}") from err
    return newest["local_path"], ndcs


def fda_to_11(ndc10: str) -> str | None:
    """FDA's 10-digit NDC (4-4-2, 5-3-2 or 5-4-1) -> the 11-digit 5-4-2 form
    CMS uses, by adding a leading zero to the short segment."""
    parts = ndc10.split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    if tuple(len(p) for p in parts) not in ((4, 4, 2), (5, 3, 2), (5, 4, 1)):
        return None
    return "-".join(p.zfill(n) for p, n in zip(parts, (5, 4, 2)))


def fetch_fda_packages(manifest: dict) -> None:
    """The FDA 10-digit NDC and package description for each crosswalk NDC."""
    crosswalk, wanted = crosswalk_ndcs()
    zip_sha256 = fda_files()[0]
    found: dict[str, list[dict]] = {}
    for row in fda_reader("package.txt", FDA_PACKAGE_COLUMNS):
        ndc10 = row["NDCPACKAGECODE"].strip()
        ndc11 = fda_to_11(ndc10)
        if ndc11 is None:
            raise RuntimeError(f"FDA package NDC {ndc10!r} is not in 4-4-2, 5-3-2 or 5-4-1 form")
        if ndc11 in wanted:
            found.setdefault(ndc11, []).append(row)
    out = io.StringIO()
    out.write("ndc_11\tndc_10\tpackage_description\tstart_marketing\tend_marketing\n")
    for ndc11, rows in sorted(found.items()):
        for row in sorted(rows, key=lambda r: r["NDCPACKAGECODE"]):
            desc = " ".join(row["PACKAGEDESCRIPTION"].split())
            out.write(f"{ndc11}\t{row['NDCPACKAGECODE'].strip()}\t{desc}\t"
                      f"{row['STARTMARKETINGDATE'].strip()}\t{row['ENDMARKETINGDATE'].strip()}\n")
    print(f"  fda_packages: {len(found):,} of {len(wanted):,} NDCs in {crosswalk} are in the FDA NDC Directory")
    save_snapshot(manifest, "fda_packages", FDA_URL, out.getvalue().encode("utf-8"), "tsv",
                  {"source_zip_sha256": zip_sha256, "crosswalk": crosswalk})


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


SOURCES = ("fda_labelers", "fda_packages", "gleif", "sec_tickers")


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
             "fda_packages": lambda: fetch_fda_packages(manifest),
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
