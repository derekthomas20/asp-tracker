"""Shared code for the company and ticker layer (data/companies.json).

data/companies.json is hand-maintained. It says which company each NDC
labeler code belongs to (the first 5 digits of an NDC in the CMS NDC-HCPCS
crosswalk), the patterns that recognise each company's names in the
crosswalk, and each company's stock tickers. It holds no numbers.

Who makes a crosswalk row:
  1. the company whose cms_names pattern matches the row's labeler name
     (CMS names the company that sells the product, which is sometimes not
     the owner of the labeler code: Harrow sells eye drugs under Novartis's
     code, "Novaplus/Hospira" is a hospital-group label on Pfizer's code);
  2. otherwise, the company that owns the labeler code;
  3. otherwise, nobody: the row is "unassigned". Nothing is guessed.
Rows from distributors and repackagers (not_manufacturers) are ignored.
Skin substitutes (HCPCS Q4100-Q4399) are left out entirely.

Each labeler code is checked against two outside sources saved by
scripts/fetch_company_sources.py:
  - the FDA NDC Directory: which company is listed under the code now;
  - GLEIF (the global Legal Entity Identifier index): who owns that company.
A code is verified when GLEIF says the FDA-listed company is, or belongs
to, the company's lei, or when the owner approved it while FDA and GLEIF
said exactly what they say now.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPANIES_PATH = ROOT / "data" / "companies.json"
SOURCES_DIR = ROOT / "reference" / "company_sources"
SOURCES_MANIFEST = ROOT / "reference" / "company_sources_manifest.json"

SKIN_SUBSTITUTE = re.compile(r"^Q4[1-3]\d\d$")
NDC_5_4_2 = re.compile(r"^(\d{5})-\d{4}-\d{2}$")
LABELER_CODE = re.compile(r"^\d{5}$")

# Verification results for one labeler code
AGREES = "agrees"              # GLEIF links the FDA-listed company to this company
NAME_MATCH = "name match"      # every company FDA lists under the code has this company's name in it
APPROVED = "approved"          # owner approved it, and FDA and GLEIF have not changed since
NEEDS_REVIEW = "needs review"  # the sources can't confirm it; the owner should look once
CONFLICT = "conflict"          # GLEIF links it to a different company in the file
VERIFIED = (AGREES, NAME_MATCH, APPROVED)

# Words left out when looking for a company's name inside an FDA name
# ("Takeda" in "Takeda Pharmaceuticals America, Inc.").
GENERIC_WORDS = {"and", "co", "inc", "the", "pharma", "pharmaceuticals"}


def normalize_name(text: str) -> str:
    """Lower case, '&' as 'and', punctuation as spaces: 'E.R. Squibb & Sons,
    L.L.C.' and 'E R SQUIBB AND SONS LLC' compare equal. Apostrophes are
    dropped (FDA also writes them as ’ or ¿): 'Dr. Reddy's' = 'Dr.Reddys'."""
    text = re.sub(r"['’¿]", "", text.lower()).replace("&", " and ")
    text = re.sub(r"\b([a-z])\.(?=[a-z]\.)", r"\1", text)  # L.L.C. -> LLC
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def load_companies(path: Path = COMPANIES_PATH) -> dict:
    """Read data/companies.json and stop on anything malformed."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    ids, owner = set(), {}
    for c in doc["companies"]:
        cid = c.get("id")
        if not cid or cid in ids:
            problems.append(f"company id {cid!r} is missing or used twice")
        ids.add(cid)
        for key in ("name", "cms_names", "labeler_codes", "lei", "sec_cik", "us_listing", "home_listing"):
            if key not in c:
                problems.append(f"{cid}: missing {key!r}")
        try:
            c["_pattern"] = re.compile(c["cms_names"])
        except (re.error, KeyError) as err:
            problems.append(f"{cid}: cms_names is not a valid pattern ({err})")
        for entry in c.get("labeler_codes", []):
            code = entry.get("code", "")
            if not LABELER_CODE.match(code):
                problems.append(f"{cid}: labeler code {code!r} is not 5 digits")
            elif code in owner:
                problems.append(f"labeler code {code} is listed under both {owner[code]} and {cid}")
            owner[code] = cid
    for entry in doc.get("not_manufacturers", []):
        code = entry.get("code", "")
        if not LABELER_CODE.match(code):
            problems.append(f"not_manufacturers: labeler code {code!r} is not 5 digits")
        elif code in owner:
            problems.append(f"labeler code {code} is in not_manufacturers and under {owner[code]}")
    if problems:
        raise SystemExit("STOPPED: data/companies.json has problems:\n  " + "\n  ".join(problems))
    doc["_by_id"] = {c["id"]: c for c in doc["companies"]}
    doc["_code_owner"] = owner
    doc["_not_manufacturers"] = {e["code"]: e for e in doc.get("not_manufacturers", [])}
    return doc


def save_companies(doc: dict, path: Path = COMPANIES_PATH) -> None:
    """Write data/companies.json back, without the helper keys load_companies adds."""
    clean = {k: v for k, v in doc.items() if not k.startswith("_")}
    clean["companies"] = [{k: v for k, v in c.items() if not k.startswith("_")} for c in doc["companies"]]
    path.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def mapped_codes(doc: dict) -> list[str]:
    return sorted(doc["_code_owner"])


def companies_for_name(doc: dict, labeler_name: str) -> list[str]:
    """Ids of every company whose cms_names pattern matches this CMS labeler
    name. More than one means the patterns overlap and must be fixed."""
    name = labeler_name.lower()
    return [c["id"] for c in doc["companies"] if c["_pattern"].search(name)]


def credit_row(doc: dict, labeler_code: str | None, labeler_name: str) -> tuple[str | None, str]:
    """Who makes one crosswalk row, and why: (company id or None, reason).
    reason is 'name', 'code', 'not a manufacturer', 'unassigned' or
    'names overlap'."""
    if labeler_code in doc["_not_manufacturers"]:
        return None, "not a manufacturer"
    by_name = companies_for_name(doc, labeler_name)
    if len(by_name) == 1:
        return by_name[0], "name"
    if len(by_name) > 1:
        return None, "names overlap"
    if labeler_code in doc["_code_owner"]:
        return doc["_code_owner"][labeler_code], "code"
    return None, "unassigned"


# --- Saved outside sources (reference/company_sources/) -------------------

def latest_snapshot(source: str) -> Path | None:
    if not SOURCES_MANIFEST.exists():
        return None
    manifest = json.loads(SOURCES_MANIFEST.read_text(encoding="utf-8"))
    snaps = manifest.get("sources", {}).get(source, {}).get("snapshots", [])
    return ROOT / snaps[-1]["local_path"] if snaps else None


def load_fda_labelers() -> dict[str, list[str]] | None:
    """Labeler code -> company names the FDA NDC Directory lists under it now."""
    path = latest_snapshot("fda_labelers")
    if path is None:
        return None
    out: dict[str, list[str]] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            out.setdefault(row["labeler_code"], []).append(row["labeler_name"])
    return out


def load_fda_packages() -> dict[str, list[dict]] | None:
    """11-digit 5-4-2 NDC (as CMS writes it) -> the FDA NDC Directory
    packages it stands for: [{ndc_10, package_description, ...}]. Usually
    one; more than one when two 10-digit NDCs pad to the same 11 digits."""
    path = latest_snapshot("fda_packages")
    if path is None:
        return None
    out: dict[str, list[dict]] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            out.setdefault(row["ndc_11"], []).append(row)
    return out


def load_gleif() -> dict[str, dict] | None:
    """Normalized company name -> what GLEIF says about it (see fetch_company_sources.py)."""
    path = latest_snapshot("gleif")
    return json.loads(path.read_text(encoding="utf-8")) if path else None


def load_sec() -> dict[int, list[dict]] | None:
    """SEC CIK -> the tickers SEC lists for it now."""
    path = latest_snapshot("sec_tickers")
    if path is None:
        return None
    out: dict[int, list[dict]] = {}
    for row in json.loads(path.read_text(encoding="utf-8")):
        out.setdefault(row["cik"], []).append(row)
    return out


def gleif_summary(fda_names: list[str], gleif: dict) -> list[list]:
    """What GLEIF says about each FDA-listed name, in a form that can be
    stored with an approval and compared later: [name, lei, parent lei]."""
    out = []
    for name in fda_names:
        rec = gleif.get(normalize_name(name)) or {}
        out.append([name, rec.get("lei"), rec.get("parent_lei")])
    return out


def name_contains(full_name: str, company_name: str) -> bool:
    """True if the company's name (without generic words) appears, as whole
    words, inside full_name: 'Sun Pharma' is in 'Sun Pharmaceutical
    Industries, Inc.' but not in 'Fosun Pharma USA'."""
    want = [w for w in normalize_name(company_name).split() if w not in GENERIC_WORDS]
    have = [w for w in normalize_name(full_name).split() if w not in GENERIC_WORDS]
    return bool(want) and any(have[i:i + len(want)] == want for i in range(len(have)))


def check_labeler(doc: dict, company: dict, entry: dict, fda: dict, gleif: dict) -> tuple[str, str]:
    """Verify one labeler code. Returns (status, plain-language detail)."""
    code = entry["code"]
    fda_names = sorted(fda.get(code, []))
    summary = gleif_summary(fda_names, gleif)
    reviewed = entry.get("reviewed")
    if reviewed and reviewed.get("fda_names") == fda_names and reviewed.get("gleif") == summary:
        return APPROVED, f"approved on {reviewed.get('date')}"

    other_leis = {c["lei"]: c["id"] for c in doc["companies"] if c["lei"] and c["id"] != company["id"]}
    notes = []
    if not fda_names:
        notes.append("FDA lists no products under this code now")
    for name, lei, parent in summary:
        rec = gleif.get(normalize_name(name)) or {}
        if company["lei"] and company["lei"] in (lei, parent):
            return AGREES, f"FDA lists {name}; GLEIF links it to {company['name']}"
        for x in (lei, parent):
            if x in other_leis:
                return CONFLICT, (f"FDA lists {name}; GLEIF links it to "
                                  f"{doc['_by_id'][other_leis[x]]['name']}, not {company['name']}")
        if not lei:
            notes.append(f"FDA lists {name}, which GLEIF does not have")
        elif parent:
            notes.append(f"FDA lists {name}; GLEIF says its parent is {rec.get('parent_name')}")
        elif rec.get("parent_exception"):
            notes.append(f"FDA lists {name}; GLEIF has no parent for it ({rec['parent_exception']})")
        else:
            notes.append(f"FDA lists {name}; GLEIF has no parent for it")
    if fda_names and all(name_contains(n, company["name"]) for n in fda_names):
        return NAME_MATCH, f"FDA lists {' / '.join(fda_names)}"
    if not company["lei"]:
        notes.append(f"{company['name']} has no lei in companies.json")
    if reviewed:
        notes.append(f"approved on {reviewed.get('date')}, but FDA or GLEIF has changed since")
    return NEEDS_REVIEW, "; ".join(notes)


def approval_record(code: str, fda: dict, gleif: dict, today: str) -> dict:
    fda_names = sorted(fda.get(code, []))
    return {"date": today, "fda_names": fda_names, "gleif": gleif_summary(fda_names, gleif)}


def check_tickers(company: dict, sec: dict, sec_by_ticker: dict) -> list[str]:
    """Problems with a company's US ticker, compared with SEC's list."""
    us = company.get("us_listing")
    cik = company.get("sec_cik")
    if cik:
        listed = sec.get(cik, [])
        if not listed:
            return [f"SEC no longer lists CIK {cik} (delisted, acquired or renamed?)"]
        tickers = [r["ticker"] for r in listed]
        if not us or us["ticker"] not in tickers:
            return [f"SEC now lists ticker {', '.join(tickers)} for CIK {cik}; companies.json says "
                    f"{us['ticker'] if us else 'none'}"]
        return []
    if us:
        hit = sec_by_ticker.get(us["ticker"])
        if hit:
            return [f"ticker {us['ticker']} is in SEC's list as CIK {hit['cik']} ({hit['name']}); add sec_cik"]
        if us.get("exchange") != "OTC":
            return [f"ticker {us['ticker']} ({us.get('exchange')}) is not in SEC's list (delisted, acquired or renamed?)"]
    return []
