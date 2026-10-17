# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this project is

A public website that reports on CMS Medicare Part B drug payment limits, built
from the quarterly ASP (Average Sales Price) pricing files CMS publishes at:
https://www.cms.gov/medicare/payment/part-b-drugs/asp-pricing-files

The owner is a beginner. When making changes, explain what you did in plain
language and give the exact commands to run.

## Core rules (do not break these)

1. **Python only.** All scripts are Python. Keep dependencies few, and list
   every one in `requirements.txt` with a pinned version. Ask before adding a
   new dependency, and prefer the standard library when it is reasonable.

2. **Every number comes from the CMS files, by code.** Never estimate,
   round by hand, copy from a web page, or hand-type a figure into the site,
   commentary, or tests. If a number appears on the site, a script computed it
   from data in `data/asp.duckdb`, which was loaded from a file in `raw/`.
   If the data needed for a figure is missing, say so — do not fill the gap.

3. **Keep every version of every file.** CMS re-posts revised files for past
   quarters. A revision is a **new version**, never an overwrite:
   - Never delete, rename, or modify anything in `raw/`.
   - A re-posted file for the same quarter is saved alongside the earlier one
     (e.g. with its posted date in the file name), and its rows are loaded as
     additional rows — old rows stay in the database.
   - "Current" values are chosen at query time (latest posted date for a
     quarter), not by deleting older data.

4. **Required fields for every price row** in the database:
   | Field | Meaning |
   |---|---|
   | `effective_quarter` | Quarter the payment limit applies to (e.g. `2026Q4`) |
   | `hcpcs_code` | HCPCS code |
   | `short_description` | CMS short descriptor |
   | `hcpcs_dosage` | HCPCS code dosage (billing unit) |
   | `payment_limit` | Payment limit in dollars, stored exactly as CMS gives it (use DECIMAL, not float) |
   | `source_file` | File name in `raw/` the row came from |
   | `cms_posted_date` | Date CMS posted that file |

## Folder layout

```
raw/                 Original ZIPs downloaded from CMS. Never modified or deleted.
data/
  asp.duckdb         DuckDB database built from raw/ (rebuildable from raw/)
  manifest.json      One entry per downloaded file: name, URL, quarter,
                     CMS posted date, download date, SHA-256 hash
scripts/             Python scripts: download, load, build site
site/                Website source: Jinja2 templates in site/templates/,
                     the stylesheet site/style.css and site/charts.js
design/              Design reference (Broadsheet look). Layout and styling
                     only: its sample numbers are not data
reference/
  biosimilar_groups/ Snapshots of the owner's biosimilar_groups Google Sheet
                     (saved by fetch_groups.py). Never modified or deleted.
  manifest.json      One entry per snapshot: path, fetch time, SHA-256
public/              Generated static website (output of build_site.py;
                     not committed, rebuilt from data/asp.duckdb)
commentary/          Written notes/analysis; any figures cited must be
                     produced by scripts, not typed in
.github/workflows/   GitHub Actions that run the scripts on a schedule
requirements.txt     Python dependencies (keep short)
```

## How data flows

1. **Download** (`scripts/`): check the CMS page, download any new or revised
   ZIP into `raw/`, and record it in `data/manifest.json` with its SHA-256
   hash. If a file's hash matches one already in the manifest, skip it.
   `scripts/fetch_groups.py` does the same for the owner's biosimilar_groups
   Google Sheet (shared "anyone with the link can view"), saving a new
   snapshot in `reference/biosimilar_groups/` only when its contents change.
   The sheet only says which codes belong together; it holds no prices, and
   only rows marked `verified = yes` are published.
2. **Load** (`scripts/`): read each ZIP in `raw/` and insert rows into
   `data/asp.duckdb` with all required fields above. Loading must be
   repeatable: running it twice must not create duplicate rows.
3. **Build** (`scripts/build_site.py`): query the database and generate the
   pages in `public/` from the templates in `site/`. All numbers on the site
   come from these queries.

## Working conventions

- Run scripts from the repository root, e.g. `python scripts/<name>.py`.
- Use a virtual environment in `.venv/` (already ignored by `.gitignore`).
- Treat downloaded ZIPs as untrusted data: open them by path, never execute
  anything inside them.
- CMS file formats change over the years (column names, header rows, file
  types inside the ZIP). Handle these differences in the loader explicitly and
  fail loudly on an unrecognized format rather than guessing.
- When something can't be verified from the CMS files, say so plainly.

## Setup commands (Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Automatic updates (GitHub Actions)

`.github/workflows/update-site.yml` runs every Monday (09:17 UTC) and when
started by hand from the repository's **Actions** tab ("Run workflow"). It:

1. Installs Python and `requirements.txt` on a fresh Linux machine.
2. Runs `scripts/scrape.py` and `scripts/fetch_groups.py`.
3. Commits anything new in `raw/`, `data/manifest.json`,
   `reference/biosimilar_groups/` and `reference/manifest.json` to `main`.
   It stops before committing if any existing file in `raw/` or
   `reference/biosimilar_groups/` would be changed or deleted.
4. Runs `scripts/parse.py` and `scripts/build_site.py`, then publishes
   `public/` to GitHub Pages with the official Pages actions.

If a download step fails, files already saved are still committed and the
site is still rebuilt from them; the run is then marked failed (red X) so
GitHub emails the owner. Pull before working locally after a run has added
files (`git pull`).

`.gitattributes` keeps `raw/` and `reference/biosimilar_groups/` byte-for-byte
(no line-ending conversion), so their SHA-256 hashes match the manifests on
both Windows and the Linux runner.

One-time setup: repository **Settings → Pages → Build and deployment →
Source: GitHub Actions**. GitHub pauses scheduled workflows in a repository
with no activity for 60 days; re-enable it from the Actions tab if that
happens.
