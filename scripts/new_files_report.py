"""Write the "new CMS file" notice for the weekly update.

Compares data/manifest.json with a copy saved before scripts/scrape.py ran.
If CMS posted new or revised ASP pricing files, writes a GitHub issue
title and body; .github/workflows/update-site.yml then opens the issue,
and GitHub emails the owner. That is the cue to write the Buttondown
newsletter. Nothing is sent to subscribers from here.

Writes nothing (and the workflow opens no issue) when no file was added.

Usage (from the repository root):
    .venv\\Scripts\\python scripts/new_files_report.py --before manifest-before.json \\
        --title-file new_files_title.txt --body-file new_files.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "data" / "manifest.json"
SITE_URL = "https://therxcurve.com/"

CATEGORY_NAMES = {
    "payment_limit": "payment limit file",
    "crosswalk": "NDC-HCPCS crosswalk",
    "noc": "NOC pricing file",
    "vaccine": "vaccine file",
}
MONTHS = {"1": "January", "2": "April", "3": "July", "4": "October"}


def quarter_name(q: str) -> str:
    """'2027Q1' -> 'January 2027' (the month the quarter starts, as CMS names its files)."""
    return f"{MONTHS[q[5]]} {q[:4]}" if len(q) == 6 and q[4] == "Q" else q


def added_files(before: dict, after: dict) -> list[dict]:
    seen = {e["local_path"] for e in before["files"]}
    return [e for e in after["files"] if e["local_path"] not in seen]


def title(files: list[dict]) -> str:
    kind = "Revised" if all(e["version"] > 1 for e in files) else "New"
    limits = sorted({e["effective_period"] for e in files if e["category"] == "payment_limit"})
    if limits:
        what = "payment limits for " + ", ".join(quarter_name(q) for q in limits)
    else:
        what = ", ".join(sorted({CATEGORY_NAMES.get(e["category"], e["category"]) for e in files}))
    return f"{kind} CMS file{'s' if len(files) > 1 else ''}: {what}"


def body(files: list[dict], owner: str | None) -> str:
    lines = []
    if owner:
        lines.append(f"@{owner}")
        lines.append("")
    lines += [
        "This week's update downloaded new or revised files from the "
        "[CMS ASP pricing files page](https://www.cms.gov/medicare/payment/part-b-drugs/asp-pricing-files), "
        "rebuilt the site and published it.",
        "",
        "| File | Quarter | New or revised | Posted by CMS |",
        "|---|---|---|---|",
    ]
    for e in sorted(files, key=lambda e: (e["effective_period"], e["category"])):
        status = "new" if e["version"] == 1 else f"revised (version {e['version']})"
        link = f"[{e['link_text']}]({e['url']})" if e.get("url") else e["link_text"]
        lines.append(f"| {link} | {quarter_name(e['effective_period'])} | {status} | "
                     f"{e.get('posted_date') or 'not listed'} |")
    lines += [
        "",
        "**Next:** check these pages, then write and send the newsletter in Buttondown.",
        "",
        f"- [Home]({SITE_URL}): the latest quarter at a glance",
        f"- [Movers]({SITE_URL}movers.html): biggest payment limit increases and decreases",
        f"- [New & dropped]({SITE_URL}changes.html): codes added to or removed from the file",
        f"- [Biosimilars]({SITE_URL}biosimilars.html)",
    ]
    if any(e["version"] > 1 for e in files):
        lines += ["", "A revised file replaces the earlier version as the current one; the earlier "
                      "version stays on each code page under \"earlier version\"."]
    lines += ["", "Close this issue once the newsletter is sent."]
    return "\n".join(lines) + "\n"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", required=True, help="copy of data/manifest.json from before scrape.py ran")
    parser.add_argument("--title-file", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--owner", help="GitHub user to @mention so the issue is emailed")
    args = parser.parse_args()

    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    files = added_files(before, after)
    if not files:
        print("No new CMS files this week.")
        return 0
    Path(args.title_file).write_text(title(files) + "\n", encoding="utf-8")
    Path(args.body_file).write_text(body(files, args.owner), encoding="utf-8")
    print(f"{len(files)} new CMS file(s): {title(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
