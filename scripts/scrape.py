"""Find and download CMS ASP pricing files.

Reads the CMS "ASP Pricing Files" page, finds every .zip link, works out what
kind of file each one is and which quarter it covers, and downloads anything
that is new or has been revised since the last run.

Every download is kept. A revised file is saved as a new file next to the
old one (the posted date is part of the file name), and data/manifest.json
gets a new entry for it. Nothing in raw/ is ever overwritten or deleted.

Usage (from the repository root):
    python scripts/scrape.py --dry-run     # show what would be downloaded
    python scripts/scrape.py               # download and update the manifest
    python scripts/scrape.py --verify-hashes   # re-download known files to
                                               # check their contents changed

Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

PAGE_URL = "https://www.cms.gov/medicare/payment/part-b-drugs/asp-pricing-files"
USER_AGENT = (
    "asp-tracker/0.1 (public research site on Medicare Part B drug payment "
    "limits; polite scraper, one request at a time)"
)
PAUSE_SECONDS = 3.0
TIMEOUT_SECONDS = 120

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
MANIFEST_PATH = ROOT / "data" / "manifest.json"

MONTH_TO_QUARTER = {"january": 1, "april": 2, "july": 3, "october": 4}

CATEGORY_LABELS = {
    "payment_limit": "Payment limit / ASP pricing file",
    "noc": "NOC pricing file",
    "crosswalk": "NDC-HCPCS crosswalk",
    "vaccine": "Seasonal vaccine file",
}


# --------------------------------------------------------------------------
# Reading the page
# --------------------------------------------------------------------------

@dataclass
class Link:
    href: str  # absolute URL as it appears on the page
    anchor_text: str  # text inside the <a> tag
    link_text: str  # anchor text plus the note after it, e.g. "– 09/17/2026 - Final file"
    section: str  # nearest heading above the link, e.g. "2026 Drug Payment Limit Files"


class _LinkCollector(HTMLParser):
    """Collects every <a> with its text and the text that follows it.

    CMS writes each file as a paragraph like:
        <p><a href="...zip">October 2026 ... Files</a> – 09/17/2026 - Final file</p>
    so the posted date sits outside the link. For each link we keep the text
    from the start of the link up to the next link or the end of the block.
    """

    BLOCK_TAGS = {"p", "li", "td", "div", "h1", "h2", "h3", "h4", "h5", "h6"}
    HEADING_TAGS = {"h2", "h3", "h4"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[Link] = []
        self._block_text: list[str] = []  # text pieces in the current block
        self._block_links: list[tuple[str, int, list[str]]] = []  # (href, start index, anchor pieces)
        self._in_anchor: list[str] | None = None
        self._heading: list[str] | None = None
        self._section = ""

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self._flush_block()
        if tag in self.HEADING_TAGS:
            self._heading = []
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._in_anchor = []
                self._block_links.append((href, len(self._block_text), self._in_anchor))

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_anchor = None
        if tag in self.HEADING_TAGS and self._heading is not None:
            self._section = _clean(" ".join(self._heading))
            self._heading = None
        if tag in self.BLOCK_TAGS:
            self._flush_block()

    def handle_data(self, data):
        self._block_text.append(data)
        if self._in_anchor is not None:
            self._in_anchor.append(data)
        if self._heading is not None:
            self._heading.append(data)

    def close(self):
        super().close()
        self._flush_block()

    def _flush_block(self):
        for i, (href, start, anchor_pieces) in enumerate(self._block_links):
            end = self._block_links[i + 1][1] if i + 1 < len(self._block_links) else len(self._block_text)
            self.links.append(Link(
                href=urllib.parse.urljoin(self.base_url, href),
                anchor_text=_clean("".join(anchor_pieces)),
                link_text=_clean("".join(self._block_text[start:end])),
                section=self._section,
            ))
        self._block_text = []
        self._block_links = []
        self._in_anchor = None


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def fetch_page(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def find_zip_links(html: str) -> list[Link]:
    parser = _LinkCollector(PAGE_URL)
    parser.feed(html)
    parser.close()
    seen = set()
    zips = []
    for link in parser.links:
        if ".zip" not in link.href.lower() or link.href in seen:
            continue
        seen.add(link.href)
        zips.append(link)
    return zips


# --------------------------------------------------------------------------
# Understanding each link
# --------------------------------------------------------------------------

def is_license_gated(url: str) -> bool:
    lowered = url.lower()
    return "/license/ama" in lowered or "apps/ama/license.asp" in lowered


def classify(text: str) -> str | None:
    """Return the file category from the link's anchor text, or None."""
    t = text.lower()
    if "vaccine" in t:
        return "vaccine"  # checked first: "NDC-HCPCS Crosswalk for Seasonal Vaccines" is a vaccine file
    if "crosswalk" in t:
        return "crosswalk"
    if "noc" in t:
        return "noc"
    if "payment limit" in t or "asp pricing" in t:
        return "payment_limit"
    return None


@dataclass
class Period:
    year: int
    label: str  # e.g. "2026Q4" or "2026-2027" (vaccine season)
    folder: str  # e.g. "Q4" or "season-2026-2027"


def effective_period(text: str, category: str | None) -> Period | None:
    """Work out the quarter (or vaccine season) a file applies to."""
    if category == "vaccine":
        m = re.search(r"\b(20\d\d)\s*[-–]\s*(20\d\d)\b", text)
        if m:
            season = f"{m.group(1)}-{m.group(2)}"
            return Period(int(m.group(1)), season, f"season-{season}")
    m = re.search(r"\b(january|april|july|october)\s+(20\d\d)\b", text, re.IGNORECASE)
    if m:
        year, q = int(m.group(2)), MONTH_TO_QUARTER[m.group(1).lower()]
        return Period(year, f"{year}Q{q}", f"Q{q}")
    # Older CMS URLs look like ".../2019-Oct-ASP-Pricing-File.zip"
    m = re.search(r"\b(20\d\d)-(jan|apr|jul|oct)", text, re.IGNORECASE)
    if m:
        year = int(m.group(1))
        q = {"jan": 1, "apr": 2, "jul": 3, "oct": 4}[m.group(2).lower()]
        return Period(year, f"{year}Q{q}", f"Q{q}")
    return None


def posted_date(link: Link) -> str | None:
    """Find the date CMS posted the file, as YYYY-MM-DD.

    Looks at the text after the link first ("– 09/17/2026 - Final file"),
    then inside the link ("... - Updated 03/04/2021"). Also handles the
    "Final file 090623" style (MMDDYY with no slashes).
    """
    trailing = link.link_text[len(link.anchor_text):]
    for text in (trailing, link.anchor_text):
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b", text)
        if m:
            parsed = _make_date(m.group(3), m.group(1), m.group(2))
            if parsed:
                return parsed
        m = re.search(r"\bfile\s+(\d{2})(\d{2})(\d{2})\b", text, re.IGNORECASE)
        if m:
            parsed = _make_date(m.group(3), m.group(1), m.group(2))
            if parsed:
                return parsed
    return None


def _make_date(year: str, month: str, day: str) -> str | None:
    y = int(year) + (2000 if len(year) == 2 else 0)
    try:
        return date(y, int(month), int(day)).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        with MANIFEST_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    return {"source_page": PAGE_URL, "files": []}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, MANIFEST_PATH)


def latest_versions(manifest: dict) -> dict[str, dict]:
    """Most recent manifest entry for each file key."""
    latest: dict[str, dict] = {}
    for entry in manifest["files"]:
        key = entry["key"]
        if key not in latest or entry["version"] > latest[key]["version"]:
            latest[key] = entry
    return latest


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------

def head(url: str) -> dict:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return {
            "etag": resp.headers.get("ETag"),
            "last_modified": resp.headers.get("Last-Modified"),
            "content_length": resp.headers.get("Content-Length"),
        }


def download(url: str) -> tuple[Path, str, int, dict]:
    """Download to a temporary file. Returns (path, sha256, size, headers)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    sha = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(suffix=".zip.part", dir=RAW_DIR)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp, os.fdopen(fd, "wb") as out:
            headers = {
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified"),
                "content_length": resp.headers.get("Content-Length"),
            }
            while chunk := resp.read(1 << 16):
                sha.update(chunk)
                out.write(chunk)
                size += len(chunk)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return Path(tmp_name), sha.hexdigest(), size, headers


def file_name_from_url(url: str) -> str:
    """'.../files/zip/july-2025-noc-pricing-file-06/23/25-final-file.zip'
    -> 'july-2025-noc-pricing-file-06-23-25-final-file.zip'"""
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    for prefix in ("/files/zip/", "/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", path.replace("/", "-")).strip("-")
    return name or "file.zip"


def destination(period: Period, posted: str | None, url: str) -> Path:
    """raw/<year>/<quarter>/<posted date>_<cms file name>, never an existing file."""
    stamp = posted or f"downloaded-{date.today().isoformat()}"
    folder = RAW_DIR / str(period.year) / period.folder
    base = f"{stamp}_{file_name_from_url(url)}"
    candidate = folder / base
    n = 2
    while candidate.exists():
        stem, suffix = os.path.splitext(base)
        candidate = folder / f"{stem}_v{n}{suffix}"
        n += 1
    return candidate


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    link: Link
    category: str
    period: Period
    posted: str | None
    key: str
    reason: str = ""  # why it will be downloaded
    previous: dict | None = None


@dataclass
class Report:
    new: list[Candidate] = field(default_factory=list)
    revised: list[Candidate] = field(default_factory=list)
    unchanged: list[Candidate] = field(default_factory=list)
    license_skipped: list[Link] = field(default_factory=list)
    unclassified: list[tuple[Link, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def plan(links: list[Link], manifest: dict, verify_hashes: bool, check_headers: bool) -> Report:
    report = Report()
    latest = latest_versions(manifest)
    keys_seen: dict[str, str] = {}

    for link in links:
        if is_license_gated(link.href):
            report.license_skipped.append(link)
            continue
        category = classify(link.anchor_text) or classify(link.href)
        period = effective_period(link.anchor_text, category) or effective_period(
            urllib.parse.unquote(link.href), category
        )
        if category is None or period is None:
            why = "unknown file type" if category is None else "no quarter found"
            report.unclassified.append((link, why))
            continue

        key = f"{period.label}|{category}"
        if category == "vaccine" and "crosswalk" in link.anchor_text.lower():
            key += "|crosswalk"  # the vaccine season has both a price file and a crosswalk
        if key in keys_seen and keys_seen[key] != link.href:
            # Two different links for the same quarter and type; keep both apart.
            key = f"{key}|{file_name_from_url(link.href)}"
        keys_seen[key] = link.href

        cand = Candidate(link, category, period, posted_date(link), key)
        prev = latest.get(key)
        cand.previous = prev

        if prev is None:
            cand.reason = "new"
            report.new.append(cand)
            continue

        changes = []
        if prev.get("posted_date") != cand.posted:
            changes.append(f"posted date {prev.get('posted_date')} -> {cand.posted}")
        if prev.get("url") != link.href:
            changes.append("URL changed")
        if not changes and (check_headers or verify_hashes):
            # Same URL and date: ask the server whether the file itself changed.
            try:
                time.sleep(PAUSE_SECONDS)
                h = head(link.href)
                if verify_hashes or any(
                    h[k] and prev.get(k) and h[k] != prev.get(k)
                    for k in ("etag", "last_modified", "content_length")
                ):
                    changes.append("check file hash")
            except urllib.error.URLError as e:
                report.errors.append(f"HEAD failed for {link.href}: {e}")
        if changes:
            cand.reason = "; ".join(changes)
            report.revised.append(cand)
        else:
            report.unchanged.append(cand)
    return report


def fetch(cand: Candidate, manifest: dict) -> str:
    """Download one file and record it. Returns 'saved' or 'same-hash'."""
    tmp, sha, size, headers = download(cand.link.href)
    prev = cand.previous
    if prev and prev.get("sha256") == sha:
        # Same bytes as before: only the page text, URL or server headers
        # changed. Record those so the next run doesn't download it again.
        tmp.unlink()
        prev.update({k: v for k, v in headers.items() if v})
        prev.update({"url": cand.link.href, "posted_date": cand.posted,
                     "anchor_text": cand.link.anchor_text, "link_text": cand.link.link_text})
        return "same-hash"

    dest = destination(cand.period, cand.posted, cand.link.href)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(tmp, dest)  # fails rather than overwrite if dest exists
    manifest["files"].append({
        "key": cand.key,
        "version": (prev["version"] + 1) if prev else 1,
        "category": cand.category,
        "effective_period": cand.period.label,
        "anchor_text": cand.link.anchor_text,
        "link_text": cand.link.link_text,
        "page_section": cand.link.section,
        "url": cand.link.href,
        "posted_date": cand.posted,
        "local_path": dest.relative_to(ROOT).as_posix(),
        "sha256": sha,
        "size_bytes": size,
        "downloaded_at": _now(),
        "change_reason": cand.reason,
        **headers,
    })
    return "saved"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def describe(c: Candidate) -> str:
    posted = c.posted or "no date on page"
    return f"  {c.period.label:<10} {CATEGORY_LABELS[c.category]:<34} posted {posted:<12} {c.link.href}"


def print_summary(report: Report, dry_run: bool) -> None:
    verb = "Would download" if dry_run else "Downloaded"
    print(f"\n{verb} — NEW files ({len(report.new)}):")
    for c in report.new:
        print(describe(c))
    print(f"\n{verb} — REVISED files ({len(report.revised)}):")
    for c in report.revised:
        print(describe(c) + f"\n      reason: {c.reason}")
    print(f"\nUnchanged since last run: {len(report.unchanged)}")

    print(f"\nSKIPPED — behind the AMA license page ({len(report.license_skipped)}):")
    for link in report.license_skipped:
        print(f"  {link.link_text}")
    if report.unclassified:
        print(f"\nSKIPPED — could not classify ({len(report.unclassified)}), please review:")
        for link, why in report.unclassified:
            print(f"  [{why}] {link.link_text}  ({link.href})")
    if report.errors:
        print(f"\nERRORS ({len(report.errors)}):")
        for e in report.errors:
            print(f"  {e}")

    counts = {}
    for c in report.new + report.revised + report.unchanged:
        counts[c.category] = counts.get(c.category, 0) + 1
    print("\nDownloadable files on the page by type:")
    for cat, label in CATEGORY_LABELS.items():
        print(f"  {label:<34} {counts.get(cat, 0)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="show what would be downloaded; change nothing")
    parser.add_argument("--verify-hashes", action="store_true",
                        help="re-download every known file and compare its SHA-256 hash")
    parser.add_argument("--no-head", action="store_true",
                        help="skip the quick server check for files whose date and URL are unchanged")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"Reading {PAGE_URL}")
    links = find_zip_links(fetch_page(PAGE_URL))
    print(f"Found {len(links)} .zip links")

    manifest = load_manifest()
    report = plan(links, manifest, args.verify_hashes, check_headers=not args.no_head)

    if not args.dry_run:
        RAW_DIR.mkdir(exist_ok=True)
        to_get = report.new + report.revised
        same, failed = [], []
        before = json.dumps(manifest, sort_keys=True)
        for i, cand in enumerate(to_get, 1):
            print(f"[{i}/{len(to_get)}] {cand.period.label} {cand.category}: {cand.link.href}")
            try:
                if fetch(cand, manifest) == "same-hash":
                    same.append(cand)
                if json.dumps(manifest, sort_keys=True) != before:
                    save_manifest(manifest)  # save after each file so a crash loses nothing
            except (urllib.error.URLError, OSError) as e:
                failed.append(cand)
                report.errors.append(f"download failed for {cand.link.href}: {e}")
            if i < len(to_get):
                time.sleep(PAUSE_SECONDS)
        # Files whose bytes matched the previous version are not real revisions.
        report.unchanged += same
        report.new = [c for c in report.new if c not in failed]
        report.revised = [c for c in report.revised if c not in same and c not in failed]
        if json.dumps(manifest, sort_keys=True) == before:
            print("\nmanifest.json unchanged (nothing new or revised).")

    print_summary(report, args.dry_run)
    if args.dry_run:
        print("\nDry run: nothing was downloaded and manifest.json was not changed.")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
