"""Download the biosimilar_groups Google Sheet and keep every version of it.

The sheet is maintained by the site owner (it is not a CMS file). It says
which biosimilar HCPCS codes belong with which reference product. It holds
no prices: every number on the site still comes from the CMS files.

Each time the sheet's contents change, a new snapshot is saved as
    reference/biosimilar_groups/<UTC date and time>_<first 12 of SHA-256>.csv
and recorded in reference/manifest.json. Snapshots are never changed or
deleted, the same way raw/ keeps every CMS file. If the sheet is unchanged
since the last snapshot, nothing is written.

The sheet must be shared as "Anyone with the link can view" so it can be
downloaded without signing in.

Usage (from the repository root):
    .venv\\Scripts\\python scripts/fetch_groups.py
Then run scripts/parse.py to load the newest snapshot into the database.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = ROOT / "reference" / "biosimilar_groups"
MANIFEST_PATH = ROOT / "reference" / "manifest.json"

SHEET_ID = "1w-2zu98UzIWpaH43pk6NxjCTkcAO_d65CNsIcHYkcK4"
SHEET_GID = "2099057208"  # the sheet tab ("Untitled")
EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"

# The first line of the CSV must be exactly this, so a changed or wrong
# sheet stops here instead of being saved.
EXPECTED_HEADER = ("molecule,hcpcs,role,brand_names,cms_description,qualifies_8pct,"
                   "valid_from,valid_to,verified,source,notes")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    request = urllib.request.Request(EXPORT_URL, headers={"User-Agent": "asp-tracker"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read()
    except urllib.error.HTTPError as err:
        print(f"STOPPED: Google returned HTTP {err.code} for the sheet.")
        print("Check that the sheet is shared as 'Anyone with the link can view'.")
        return 1

    # A private sheet answers with a sign-in web page instead of CSV.
    if "text/csv" not in content_type:
        print(f"STOPPED: expected CSV but got {content_type or 'no content type'}.")
        print("Check that the sheet is shared as 'Anyone with the link can view'.")
        return 1
    text = data.decode("utf-8-sig")
    header = text.splitlines()[0].strip() if text else ""
    if header != EXPECTED_HEADER:
        print("STOPPED: the sheet's columns are not the expected ones.")
        print(f"  expected: {EXPECTED_HEADER}")
        print(f"  found:    {header}")
        return 1

    sha256 = hashlib.sha256(data).hexdigest()
    manifest = {"source": EXPORT_URL, "snapshots": []}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest["snapshots"] and manifest["snapshots"][-1]["sha256"] == sha256:
        print(f"No change since {manifest['snapshots'][-1]['local_path']}. Nothing written.")
        return 0

    fetched = datetime.now(timezone.utc).replace(microsecond=0)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{fetched:%Y-%m-%dT%H%M%SZ}_{sha256[:12]}.csv"
    if path.exists():
        print(f"STOPPED: {path.relative_to(ROOT)} already exists; not overwriting it.")
        return 1
    path.write_bytes(data)

    manifest["snapshots"].append({
        "local_path": path.relative_to(ROOT).as_posix(),
        "fetched_at": fetched.isoformat(),
        "sha256": sha256,
        "size_bytes": len(data),
    })
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {path.relative_to(ROOT)} ({len(data):,} bytes).")
    print("Now run scripts/parse.py to load it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
