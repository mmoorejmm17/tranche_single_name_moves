#!/usr/bin/env python3
"""
download_filings.py - download a company's credit agreements, indentures and
amendments from SEC EDGAR into companies/<ticker>/filings/.

Usage (run on your own machine, where the S: drive is reachable):
    python download_filings.py XRX --user-agent "Your Name your.email@firm.com"
    python download_filings.py MPW --since 2022-01-01
    python download_filings.py 1770450 --dry-run          # CIK also works

The SEC requires every automated request to carry a User-Agent with a name
and contact email, and asks for no more than 10 requests per second. This
script sends a descriptive User-Agent and waits ~0.2s between requests.
You can set the environment variable SEC_USER_AGENT instead of --user-agent.

How it finds documents: EDGAR full-text search for phrases such as
"Credit Agreement" and "Indenture" in the company's 8-K, 10-K and 10-Q
filings, keeping the exhibits (EX-4.x indentures, EX-10.x agreements) plus
the body of 8-Ks that report a material agreement (Item 1.01).

Output (in companies/<ticker>/filings/):
    <ticker>_<date>_<form>_<exhibit>_<original name>   the downloaded files
    filings_manifest.csv / filings_manifest.md          index with source URLs
Existing files are skipped, so re-running only adds new filings.
The "doc type" column is a keyword guess from the first page of text;
always check the document itself.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SEC_WWW = "https://www.sec.gov"
EFTS = "https://efts.sec.gov/LATEST/search-index"
DEFAULT_QUERIES = [
    "Credit Agreement",
    "Indenture",
    "Loan Agreement",
    "Supplemental Indenture",
    "Note Purchase Agreement",
    "Exchange Support Agreement",
]
DEFAULT_FORMS = "8-K,10-K,10-Q"
MIN_INTERVAL = 0.2  # seconds between requests (SEC limit is 10/second)
PAGE = 100          # hits per search page

# (label, regex) checked against the first ~3000 characters of each document.
DOC_PATTERNS = [
    ("Supplemental Indenture", r"SUPPLEMENTAL\s+INDENTURE"),
    ("Indenture", r"\bINDENTURE\b"),
    ("Credit Agreement", r"CREDIT\s+AGREEMENT"),
    ("Loan Agreement", r"LOAN\s+AGREEMENT"),
    ("Note Purchase Agreement", r"NOTE\s+PURCHASE\s+AGREEMENT"),
    ("Exchange/Support Agreement", r"EXCHANGE\s+SUPPORT\s+AGREEMENT|SUPPORT\s+AGREEMENT"),
    ("Security/Collateral Agreement", r"SECURITY\s+AGREEMENT|COLLATERAL\s+AGREEMENT"),
    ("Intercreditor", r"INTERCREDITOR"),
    ("Amendment", r"\bAMENDMENT\s+NO|\bAMENDMENT\b"),
]

_last_request = 0.0


def http_get(url, user_agent, retries=4):
    """GET with rate limiting, a User-Agent, and retries on throttling."""
    global _last_request
    for attempt in range(retries):
        wait = MIN_INTERVAL - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        req = Request(url, headers={"User-Agent": user_agent,
                                    "Accept-Encoding": "identity"})
        try:
            with urlopen(req, timeout=60) as resp:
                return resp.read()
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
        except URLError:
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise


def resolve_company(ident, user_agent):
    """Return (cik:int, ticker:str, title:str) for a ticker or numeric CIK."""
    data = json.loads(http_get(SEC_WWW + "/files/company_tickers.json", user_agent))
    entries = list(data.values())
    if ident.isdigit():
        cik = int(ident)
        for e in entries:
            if int(e["cik_str"]) == cik:
                return cik, e["ticker"].upper(), e["title"]
        return cik, ident, ident
    for e in entries:
        if e["ticker"].upper() == ident.upper():
            return int(e["cik_str"]), e["ticker"].upper(), e["title"]
    sys.exit(f"Ticker {ident!r} not found in SEC ticker list. Try the numeric CIK.")


def search_hits(cik, query, forms, since, until, user_agent):
    """Yield raw EDGAR full-text-search hits for one phrase."""
    start = 0
    while start < 10000:
        params = {
            "q": f'"{query}"',
            "ciks": f"{cik:010d}",
            "forms": forms,
            "dateRange": "custom",
            "startdt": since,
            "enddt": until,
            "from": start,
        }
        raw = http_get(EFTS + "?" + urlencode(params), user_agent)
        hits = json.loads(raw).get("hits", {})
        batch = hits.get("hits", [])
        for h in batch:
            yield h
        total = hits.get("total", {}).get("value", 0)
        start += PAGE
        if not batch or start >= total:
            break


def parse_hit(hit):
    """Turn a raw hit into a flat dict, or None if it has no filename."""
    doc_id = hit.get("_id", "")
    if ":" not in doc_id:
        return None
    adsh, fname = doc_id.split(":", 1)
    src = hit.get("_source", {})
    return {
        "adsh": adsh,
        "fname": fname,
        "file_type": src.get("file_type") or "",
        "form": src.get("form") or "",
        "file_date": src.get("file_date") or "",
        "items": src.get("items") or [],
        "description": src.get("file_description") or "",
    }


def wanted(rec, include_main_docs=True):
    """Keep indentures/agreements (EX-4, EX-10) and 8-K bodies with Item 1.01."""
    ft = rec["file_type"].upper()
    if ft.startswith("EX-4") or ft.startswith("EX-10"):
        return True
    if include_main_docs and ft == rec["form"].upper() and ft.startswith("8-K"):
        return "1.01" in rec["items"]
    return False


def build_url(cik, rec):
    return f"{SEC_WWW}/Archives/edgar/data/{cik}/{rec['adsh'].replace('-', '')}/{rec['fname']}"


def local_name(ticker, rec):
    base = "_".join([ticker.lower(), rec["file_date"], rec["form"].replace("/", "-"),
                     rec["file_type"] or "doc", rec["fname"]])
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)


def guess_doc_type(raw_bytes):
    """Keyword guess of what the document is, from the start of its text."""
    text = raw_bytes[:60000].decode("utf-8", errors="ignore")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;|\s+", " ", text)
    head = text[:3000].upper()
    labels = [lab for lab, pat in DOC_PATTERNS if re.search(pat, head)]
    return " / ".join(labels[:3]) if labels else "Other (check)"


def write_manifests(folder, rows):
    rows = sorted(rows, key=lambda r: (r["file_date"], r["adsh"], r["file_type"]))
    cols = ["file_date", "form", "file_type", "doc_type_guess", "description",
            "items", "local_file", "url"]
    with open(folder / "filings_manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(folder / "filings_manifest.md", "w", encoding="utf-8") as f:
        f.write(f"# Filing manifest (generated {date.today()})\n\n")
        f.write("| Date | Form | Exhibit | Doc type (guess) | File | Source |\n|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['file_date']} | {r['form']} | {r['file_type']} | "
                    f"{r['doc_type_guess']} | {r['local_file']} | [EDGAR]({r['url']}) |\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("company", help="ticker (e.g. XRX) or numeric CIK")
    ap.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT"),
                    help='required by the SEC, e.g. "Your Name you@firm.com"')
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--until", default=str(date.today()))
    ap.add_argument("--queries", default=",".join(DEFAULT_QUERIES),
                    help="comma-separated phrases to search for")
    ap.add_argument("--forms", default=DEFAULT_FORMS)
    ap.add_argument("--root", default=None,
                    help="companies folder (default: <script folder>/companies)")
    ap.add_argument("--no-main-docs", action="store_true",
                    help="skip the body of 8-Ks with Item 1.01")
    ap.add_argument("--dry-run", action="store_true", help="list only, no download")
    args = ap.parse_args()

    if not args.user_agent or "@" not in args.user_agent:
        sys.exit('The SEC requires a User-Agent with a contact email. Use '
                 '--user-agent "Your Name you@firm.com" or set SEC_USER_AGENT.')

    cik, ticker, title = resolve_company(args.company, args.user_agent)
    print(f"{title} ({ticker}), CIK {cik}")

    root = Path(args.root) if args.root else Path(__file__).resolve().parent / "companies"
    folder = root / ticker.lower() / "filings"

    found = {}
    for q in [s.strip() for s in args.queries.split(",") if s.strip()]:
        n = 0
        for hit in search_hits(cik, q, args.forms, args.since, args.until, args.user_agent):
            rec = parse_hit(hit)
            if rec and wanted(rec, not args.no_main_docs):
                key = (rec["adsh"], rec["fname"])
                if key not in found:
                    found[key] = rec
                    n += 1
        print(f"  search '{q}': {n} new documents")

    recs = sorted(found.values(), key=lambda r: (r["file_date"], r["adsh"]))
    print(f"{len(recs)} documents to consider.")

    if args.dry_run:
        for r in recs:
            print(f"  {r['file_date']}  {r['form']:6} {r['file_type']:8} {build_url(cik, r)}")
        return

    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for r in recs:
        url = build_url(cik, r)
        name = local_name(ticker, r)
        path = folder / name
        if path.exists():
            raw = path.read_bytes()
            status = "exists"
        else:
            try:
                raw = http_get(url, args.user_agent)
            except (HTTPError, URLError) as e:
                print(f"  FAILED {url}: {e}")
                continue
            path.write_bytes(raw)
            status = "downloaded"
        r.update({"doc_type_guess": guess_doc_type(raw), "local_file": name, "url": url})
        rows.append(r)
        print(f"  {status:10} {name}  [{r['doc_type_guess']}]")

    write_manifests(folder, rows)
    print(f"Done. {len(rows)} files in {folder}")


if __name__ == "__main__":
    main()
