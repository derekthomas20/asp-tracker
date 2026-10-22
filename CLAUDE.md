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
raw/                 Original files downloaded from CMS. Never modified or deleted.
  <year>/<quarter>/  ASP pricing ZIPs (scrape.py)
  data-cms-gov/      Medicare usage CSVs from data.cms.gov (fetch_cms_data.py),
                     by dataset and period. The ~40 MB Geography and Service
                     files are stored gzip-compressed (lossless; parse.py
                     checks the decompressed bytes against the manifest hash)
data/
  asp.duckdb         DuckDB database built from raw/ (rebuildable from raw/)
  manifest.json      One entry per downloaded file: name, URL, quarter,
                     CMS posted date, download date, SHA-256 hash
  cms_data_manifest.json  The same for the data.cms.gov files: dataset,
                     period, URL, date CMS last changed it, SHA-256
  ncr_markets.json   Hand-maintained markets for the NCR calculator page:
                     default doses (with their FDA label source), which
                     packages to use, multi-dose vials, example contract
                     prices. Products come from the biosimilar groups sheet;
                     package sizes and payment limits come from asp.duckdb
  site.json          Hand-maintained: who runs the site, contact email, and
                     the Buttondown username for the email sign-up shown on
                     every page. Blank fields leave their part of the site
                     off. No numbers
  companies.json     Hand-maintained: which company owns each NDC labeler
                     code, patterns for each company's names in the CMS
                     crosswalk, and stock tickers (US and home market).
                     No numbers. Checked weekly by companies_report.py
scripts/             Python scripts: download, load, build site
site/                Website source: Jinja2 templates in site/templates/,
                     the stylesheet site/style.css and site/charts.js, and
                     the Rxcurve logo and favicons in site/static/ (PNGs drawn
                     by scripts/make_favicons.py)
design/              Design reference (Broadsheet layout; the site is branded
                     Rxcurve). Layout only: its sample numbers are not data
reference/
  biosimilar_groups/ Snapshots of the owner's biosimilar_groups Google Sheet
                     (saved by fetch_groups.py). Never modified or deleted.
  manifest.json      One entry per snapshot: path, fetch time, SHA-256
  company_sources/   Extracts that check data/companies.json (saved by
                     fetch_company_sources.py; never modified or deleted):
                     fda_labelers/ (FDA NDC Directory names per labeler
                     code), fda_packages/ (FDA 10-digit NDC and package
                     description for each NDC in the newest CMS crosswalk;
                     shown on code pages), gleif/ (legal entity and ultimate parent),
                     sec_tickers/ (SEC ticker list)
  company_sources_manifest.json  One entry per extract snapshot
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
   `scripts/fetch_cms_data.py` reads the data.cms.gov catalog
   (https://data.cms.gov/data.json) and downloads new or revised CSVs for
   four usage datasets: Part B Spending by Drug (yearly), Quarterly Part B
   Spending by Drug, Part B Discarded Drug Units, and Physician & Other
   Practitioners by Geography and Service (2021 onward). Not downloaded:
   OPPS Addendum B (behind the AMA license page, like the AMA-gated ASP
   files) and the discarded-drug refund reports (PDF only).
   `scripts/fetch_company_sources.py` saves small extracts of the FDA NDC
   Directory, GLEIF and SEC's ticker list for the companies in
   `data/companies.json` (needs the `SEC_CONTACT_EMAIL` environment
   variable; SEC requires a contact email).
   `scripts/companies_report.py` then checks every labeler code: it is
   verified when GLEIF links the FDA-listed company to the company's LEI,
   or when the owner approved it (`--approve <code>`) and FDA and GLEIF
   have not changed since. Everything else, plus ticker problems and
   unassigned labelers with spending, goes into one GitHub issue labelled
   `company-review`, updated weekly. A crosswalk row is credited to the
   company whose `cms_names` pattern matches its labeler name, else to the
   labeler code's owner, else to nobody ("unassigned"; never guessed).
   Skin substitutes (Q4100-Q4399) are left out.
2. **Load** (`scripts/`): read each ZIP in `raw/` and insert rows into
   `data/asp.duckdb` with all required fields above. Loading must be
   repeatable: running it twice must not create duplicate rows.
   `scripts/cms_data.py` (called by `parse.py`) loads the data.cms.gov files
   into `part_b_spending`, `part_b_spending_quarterly`,
   `discarded_drug_units` and `physician_geo_service`. Later releases repeat
   earlier periods (each yearly spending file holds five years), so queries
   should use `is_latest_release` to count each period once.
3. **Build** (`scripts/build_site.py`): query the database and generate the
   pages in `public/` from the templates in `site/`. All numbers on the site
   come from these queries. The NCR calculator also gets
   `tools/ncr-calculator/products.js`: every product in the latest CMS
   crosswalk (payment limit, billing unit, package sizes), so visitors can
   build their own comparisons; they enter doses for products not in
   `data/ncr_markets.json`. Company pages (`companies.html`,
   `companies/<id>.html`) use only verified labeler codes; Medicare
   spending is added up only for codes every crosswalk product of which is
   credited to that one company. Shared-code spending is never split between
   makers; a company whose shared codes have more spending (all makers
   together) than its own codes is listed separately as "mostly shared".

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
2. Runs `scripts/scrape.py`, `scripts/fetch_cms_data.py`,
   `scripts/fetch_groups.py` and `scripts/fetch_company_sources.py`.
3. Commits anything new in `raw/`, `data/manifest.json`,
   `data/cms_data_manifest.json`, `reference/biosimilar_groups/`,
   `reference/manifest.json`, `reference/company_sources/` and
   `reference/company_sources_manifest.json` to `main`.
   It stops before committing if any existing file in `raw/`,
   `reference/biosimilar_groups/` or `reference/company_sources/` would be
   changed or deleted.
4. Runs `scripts/parse.py` and `scripts/build_site.py`, then publishes
   `public/` to GitHub Pages with the official Pages actions.
5. Runs `scripts/companies_report.py` and opens, updates or closes the
   "Company mapping needs review" issue (label `company-review`). The
   issue gets a comment, which GitHub emails, only when its list changes.

If a download step fails, files already saved are still committed and the
site is still rebuilt from them; the run is then marked failed (red X) so
GitHub emails the owner. Pull before working locally after a run has added
files (`git pull`).

`.github/workflows/deploy.yml` runs on every push to `main`, and can be
started by hand from the **Actions** tab ("Deploy site" → "Run workflow")
if a push ever fails to start it. It downloads
nothing: it rebuilds `data/asp.duckdb` from `raw/` with `scripts/parse.py`,
runs `scripts/build_site.py`, and publishes `public/` to GitHub Pages. (The
weekly workflow's own commits don't trigger it; that workflow publishes the
site itself.)

`.gitattributes` keeps `raw/` and `reference/biosimilar_groups/` byte-for-byte
(no line-ending conversion), so their SHA-256 hashes match the manifests on
both Windows and the Linux runner.

One-time setup: repository **Settings → Pages → Build and deployment →
Source: GitHub Actions**, and **Settings → Secrets and variables →
Actions → Variables → New repository variable** named `SEC_CONTACT_EMAIL`
holding the contact email SEC requires. GitHub pauses scheduled workflows in a repository
with no activity for 60 days; re-enable it from the Actions tab if that
happens.
