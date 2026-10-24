"""Save the newsletter for a new quarter to Buttondown as a DRAFT.

Runs in the weekly update after scripts/new_files_report.py. When this
run added a new payment limit file for the quarter the site now shows,
it takes the email scripts/build_site.py wrote to build/newsletter.json
and saves it in Buttondown as a draft. Nothing is sent: the owner reads
the draft in Buttondown and presses Send.

Safety: Buttondown sends an email straight away unless told otherwise
(its default status is "about_to_send"), so the request always asks for
status "draft", and the script stops with an error if Buttondown answers
with any other status.

Needs the BUTTONDOWN_API_KEY environment variable (on GitHub, a repository
secret of that name; the key is in Buttondown under Settings > API). Without
it, nothing is saved and the note says so, with the email text, so it can
be pasted into Buttondown by hand.

Writes a short Markdown note (--note-file) for the GitHub issue either way,
or nothing when there is no new quarter to write about (a revised file for
a past quarter, for example).

Usage (from the repository root, after build_site.py):
    .venv\\Scripts\\python scripts/newsletter_draft.py --before manifest-before.json --note-file note.md
    .venv\\Scripts\\python scripts/newsletter_draft.py --before manifest-before.json --note-file note.md --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "data" / "manifest.json"
NEWSLETTER = ROOT / "build" / "newsletter.json"
API_URL = "https://api.buttondown.com/v1/emails"
DRAFTS_PAGE = "https://buttondown.com/emails"
TIMEOUT_SECONDS = 60


def new_quarters(before: dict, after: dict) -> set[str]:
    """Quarters with a first (not revised) payment limit file added this run."""
    seen = {e["local_path"] for e in before["files"]}
    return {e["effective_period"] for e in after["files"]
            if e["local_path"] not in seen and e["category"] == "payment_limit" and e["version"] == 1}


def save_draft(api_key: str, subject: str, body: str) -> dict:
    request = urllib.request.Request(
        API_URL, method="POST",
        data=json.dumps({"subject": subject, "body": body, "status": "draft"}).encode("utf-8"),
        headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json",
                 "User-Agent": "rxcurve newsletter_draft.py"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", required=True, help="copy of data/manifest.json from before scrape.py ran")
    parser.add_argument("--note-file", required=True, help="where to write the note for the GitHub issue")
    parser.add_argument("--dry-run", action="store_true", help="write the note but never contact Buttondown")
    args = parser.parse_args()

    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    quarters = new_quarters(before, after)
    if not NEWSLETTER.exists():
        raise SystemExit(f"STOPPED: {NEWSLETTER.relative_to(ROOT)} not found; run scripts/build_site.py first.")
    draft = json.loads(NEWSLETTER.read_text(encoding="utf-8"))
    if draft["quarter"] not in quarters:
        print(f"No new payment limit file for {draft['quarter']} this run; no newsletter draft.")
        return 0

    pasted = ("<details><summary>Email text</summary>\n\n**Subject:** " + draft["subject"]
              + "\n\n```markdown\n" + draft["body"] + "```\n</details>\n")
    api_key = os.environ.get("BUTTONDOWN_API_KEY", "").strip()
    if args.dry_run or not api_key:
        reason = "dry run" if args.dry_run else "no BUTTONDOWN_API_KEY secret is set"
        note = (f"**Newsletter draft not saved to Buttondown** ({reason}). To send it, start a new email "
                f"in Buttondown and paste the text below.\n\n" + pasted)
        print(f"Newsletter draft not saved ({reason}).")
    else:
        try:
            saved = save_draft(api_key, draft["subject"], draft["body"])
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:500]
            Path(args.note_file).write_text(
                f"**Saving the newsletter draft failed** (Buttondown said HTTP {err.code}: {detail}). "
                f"Paste the text below into a new Buttondown email instead.\n\n" + pasted, encoding="utf-8")
            print(f"FAILED: Buttondown answered HTTP {err.code}: {detail}")
            return 1
        status = saved.get("status")
        if status != "draft":
            # Should never happen; if it does, say so loudly.
            Path(args.note_file).write_text(
                f"**WARNING:** Buttondown saved the newsletter with status `{status}`, not `draft`. "
                f"Check {DRAFTS_PAGE} right away.\n", encoding="utf-8")
            print(f"FAILED: Buttondown saved the email with status {status!r}, not 'draft'.")
            return 1
        note = (f"**Newsletter draft saved in Buttondown:** \"{draft['subject']}\". Open it from the drafts in "
                f"[Buttondown]({DRAFTS_PAGE}), read it, edit anything you like, then send it. "
                f"Nothing has been sent.\n")
        print(f"Saved Buttondown draft {saved.get('id')}: {draft['subject']}")
    Path(args.note_file).write_text(note, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
