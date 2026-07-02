import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

try:
    import fitz
except ImportError:
    fitz = None


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
DATA_DIR = ROOT_DIR / "data"
CONFIG_DIR = ROOT_DIR / "config"
REGISTRY_PATH = CONFIG_DIR / "research_cache_registry.json"

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) "
        "AppleWebKit/537.36 Chrome/125 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}

SCREENER_TABLE_IDS = (
    "quarters",
    "profit-loss",
    "balance-sheet",
    "cash-flow",
    "ratios",
    "shareholding",
)

MARKET_CAP_PROFILES = {
    "large": {
        "weights": {
            "management": 15,
            "moat": 25,
            "growth": 20,
            "financial_quality": 40,
        },
        "rule": "ROCE + FCF + Moat > Growth",
        "fast_reject_rules": [
            "ROCE < 10%",
            "FCF negative for years",
            "Debt exploding",
        ],
        "primary_focus": "Quality, cash flow, moat, and capital allocation.",
    },
    "mid": {
        "weights": {
            "management": 10,
            "moat": 15,
            "growth": 40,
            "financial_quality": 35,
        },
        "rule": "Growth + ROCE together",
        "fast_reject_rules": [
            "Revenue growth < 10%",
            "ROCE < 12%",
            "Dilution high",
        ],
        "primary_focus": "Growth, ROCE, positive FCF, and execution quality.",
    },
    "small": {
        "weights": {
            "management": 35,
            "moat": 15,
            "growth": 35,
            "financial_quality": 15,
        },
        "rule": "Management > Business > Numbers",
        "fast_reject_rules": [
            "Promoter/management questionable",
            "Constant equity dilution",
            "Debt-heavy balance sheet",
            "No clear moat or niche",
        ],
        "primary_focus": "Management quality, TAM, scalability, moat, and growth.",
    },
    "unknown": {
        "weights": {
            "management": 25,
            "moat": 20,
            "growth": 30,
            "financial_quality": 25,
        },
        "rule": "Unknown cap bucket: use balanced evidence until market-cap rank is known.",
        "fast_reject_rules": [],
        "primary_focus": "Balanced fallback profile.",
    },
}

CHECKLIST_TERMS = {
    "revenue_growth_5_10y": ["revenue", "income from operations", "sales", "turnover", "cagr"],
    "eps_growth_5_10y": ["earnings per share", "eps", "diluted eps", "basic eps"],
    "operating_profit_cagr": ["operating profit", "ebitda", "profit before tax", "pbit"],
    "fcf_and_cash_conversion": ["free cash flow", "cash flow from operating", "operating cash flow", "cash conversion"],
    "roce": ["return on capital employed", "roce"],
    "roe": ["return on equity", "roe"],
    "operating_margin": ["operating margin", "ebitda margin", "margin"],
    "gross_margin": ["gross margin", "gross profit"],
    "debt_levels": ["debt", "borrowings", "debt equity", "debt/equity", "finance cost"],
    "interest_coverage": ["interest coverage", "finance costs", "interest expense"],
    "shares_outstanding_dilution": ["share capital", "equity shares", "dilution", "outstanding shares"],
    "market_share": ["market share", "leadership", "ranked", "largest", "position"],
    "competitive_advantage_moat": ["competitive advantage", "moat", "entry barrier", "differentiated"],
    "pricing_power": ["pricing power", "price increase", "premium", "realisation", "realization"],
    "brand_strength": ["brand", "brands", "consumer trust"],
    "switching_costs": ["switching cost", "stickiness", "retention", "long-term contract"],
    "network_effects": ["network effect", "platform", "ecosystem"],
    "management_quality": ["management", "board", "leadership", "governance"],
    "capital_allocation": ["capital allocation", "capex", "dividend", "buyback", "return capital"],
    "buyback_history": ["buyback", "share repurchase"],
    "insider_promoter_ownership": ["promoter", "shareholding", "insider", "pledge"],
    "acquisition_discipline": ["acquisition", "merger", "integration", "purchase consideration"],
    "shareholder_communication": ["shareholders", "letter", "communication", "annual general meeting"],
    "promised_vs_delivered": ["guidance", "outlook", "target", "promise", "commitment", "achieved", "delivered"],
    "r_and_d_efficiency": ["research and development", "r&d", "innovation", "patent"],
    "customer_concentration": ["customer concentration", "major customer", "top customer", "client concentration"],
    "industry_tailwind_tam": ["industry", "tailwind", "market opportunity", "tam", "addressable market"],
    "valuation": ["valuation", "market capitalisation", "market capitalization", "price earnings", "p/e"],
    "monopoly_potential": ["monopoly", "niche", "dominant", "sole", "exclusive"],
    "order_book_growth": ["order book", "orders", "backlog"],
    "customer_growth": ["customer growth", "new customers", "clients"],
    "product_adoption": ["adoption", "new product", "launch"],
    "recurring_revenue": ["recurring revenue", "subscription", "annuity"],
    "cash_position": ["cash and cash equivalents", "cash balance", "bank balances"],
    "working_capital_efficiency": ["working capital", "inventory", "receivables", "payables"],
    "patent_portfolio": ["patent", "intellectual property", "ip portfolio"],
    "niche_leadership": ["niche", "leadership", "specialised", "specialized"],
}

FOCUS_PAGES = {
    "business_overview": 12,
    "mda": 85,
    "related_party_transactions": 132,
    "financial_statements": 210,
    "auditor_remarks": 245,
}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def quarter_key(dt):
    quarter = ((dt.month - 1) // 3) + 1
    return f"{dt.year}-Q{quarter}"


def timestamp_for_path(path):
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat()


def clean_text(text):
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compact_snippet(text, term, radius=650):
    lowered = text.lower()
    idx = lowered.find(term.lower())
    if idx < 0:
        return clean_text(text[: radius * 2])

    start = max(0, idx - radius)
    end = min(len(text), idx + len(term) + radius)
    return clean_text(text[start:end])


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_csv_symbols(path):
    if not path.exists():
        return []

    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            symbol = (row.get("SYMBOL") or row.get("Symbol") or "").strip().upper()
            company = (row.get("Company Name") or "").strip()
            industry = (row.get("Industry") or "").strip()
            if symbol:
                rows.append({"symbol": symbol, "company_name": company, "industry": industry})
    return rows


def build_market_cap_lookup(config_dir):
    lookup = {}

    nifty100 = load_csv_symbols(config_dir / "nifty100.csv")
    for rank, row in enumerate(nifty100, start=1):
        lookup[row["symbol"]] = {
            "category": "large",
            "source": "config/nifty100.csv",
            "rank_hint": rank,
            "company_name": row["company_name"],
            "industry": row["industry"],
        }

    nifty500 = load_csv_symbols(config_dir / "ind_nifty500list.csv")
    for rank, row in enumerate(nifty500, start=1):
        if rank <= 100:
            category = "large"
        elif rank <= 250:
            category = "mid"
        else:
            category = "small"

        current = lookup.get(row["symbol"])
        if current and current["category"] == "large":
            continue

        lookup[row["symbol"]] = {
            "category": category,
            "source": "config/ind_nifty500list.csv rank position",
            "rank_hint": rank,
            "company_name": row["company_name"],
            "industry": row["industry"],
        }

    small250 = load_csv_symbols(config_dir / "ind_niftysmallcap250list.csv")
    for rank, row in enumerate(small250, start=1):
        lookup.setdefault(
            row["symbol"],
            {
                "category": "small",
                "source": "config/ind_niftysmallcap250list.csv",
                "rank_hint": 250 + rank,
                "company_name": row["company_name"],
                "industry": row["industry"],
            },
        )

    return lookup


def parse_symbols(items):
    selected = []
    seen = set()
    for item in items or []:
        for symbol in item.split(","):
            symbol = symbol.strip().upper()
            if symbol and symbol not in seen:
                selected.append(symbol)
                seen.add(symbol)
    return selected


def ticker_dirs(data_dir, selected):
    dirs = sorted(path for path in data_dir.iterdir() if path.is_dir())
    if selected:
        selected_set = set(selected)
        dirs = [path for path in dirs if path.name.upper() in selected_set]
    return dirs


def extract_pdf_pages(path, max_pages):
    if fitz is None:
        raise RuntimeError("PyMuPDF/fitz is not installed.")

    pages = []
    with fitz.open(path) as doc:
        if doc.page_count <= 0:
            raise RuntimeError("PDF opened but reported zero pages.")

        page_limit = min(doc.page_count, max_pages)
        for index in range(page_limit):
            page = doc.load_page(index)
            text = clean_text(page.get_text("text"))
            pages.append({"page": index + 1, "text": text})
    return pages


def pdf_has_pages(path):
    if fitz is None or not path.exists() or path.stat().st_size <= 1000:
        return False

    try:
        with fitz.open(path) as doc:
            return doc.page_count > 0
    except Exception:
        return False


def find_checklist_snippets(pages, max_snippets_per_item):
    lowered_pages = [{"page": row["page"], "text": row["text"], "lower": row["text"].lower()} for row in pages]
    evidence = {}

    for item, terms in CHECKLIST_TERMS.items():
        hits = []
        for page in lowered_pages:
            for term in terms:
                if term.lower() in page["lower"]:
                    hits.append(
                        {
                            "page": page["page"],
                            "matched_term": term,
                            "snippet": compact_snippet(page["text"], term),
                        }
                    )
                    break

            if len(hits) >= max_snippets_per_item:
                break

        evidence[item] = hits

    return evidence


def extract_focus_pages(pages):
    page_by_number = {row["page"]: row["text"] for row in pages}
    focused = {}

    for key, page_number in FOCUS_PAGES.items():
        text = page_by_number.get(page_number, "")
        focused[key] = {
            "page": page_number,
            "found": bool(text),
            "snippet": clean_text(text[:2200]) if text else "",
        }

    return focused


def annual_year_from_path(path):
    match = re.search(r"(\d{4})[_-](\d{4})", path.stem)
    if match:
        return f"{match.group(1)}_{match.group(2)}"
    return path.stem


def cache_annual_report(ticker_dir, pdf_path, profile, max_snippets_per_item, max_pdf_pages, force):
    year_key = annual_year_from_path(pdf_path)
    cache_dir = ticker_dir / "research_cache" / "annual_reports"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{year_key}.json"
    pdf_hash = sha256_file(pdf_path)

    if cache_path.exists() and not force:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("source", {}).get("sha256") == pdf_hash and cached.get("page_count", 0) > 0:
                return cache_path, cached
        except json.JSONDecodeError:
            pass

    pages = extract_pdf_pages(pdf_path, max_pdf_pages)
    cache = {
        "schema_version": 1,
        "ticker": ticker_dir.name,
        "source_type": "annual_report",
        "year": year_key,
        "created_at": utc_now(),
        "market_cap_profile": profile,
        "source": {
            "pdf_path": str(pdf_path.relative_to(ROOT_DIR)),
            "sha256": pdf_hash,
            "size_bytes": pdf_path.stat().st_size,
        },
        "page_count": len(pages),
        "text_page_count": sum(1 for page in pages if page["text"]),
        "focus_pages": extract_focus_pages(pages),
        "checklist_evidence": find_checklist_snippets(pages, max_snippets_per_item),
    }

    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return cache_path, cache


def transcript_key(url, fallback_file):
    if url:
        return sha256_bytes(url.encode("utf-8"))[:16]
    return sha256_bytes(str(fallback_file).encode("utf-8"))[:16]


def cache_concall(ticker_dir, pdf_path, metadata_row, profile, max_snippets_per_item, max_pdf_pages, force):
    url = metadata_row.get("url") or ""
    key = transcript_key(url, pdf_path)
    cache_dir = ticker_dir / "research_cache" / "concalls"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{key}.json"
    pdf_hash = sha256_file(pdf_path)

    if cache_path.exists() and not force:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("source", {}).get("sha256") == pdf_hash and cached.get("page_count", 0) > 0:
                return cache_path, cached
        except json.JSONDecodeError:
            pass

    pages = extract_pdf_pages(pdf_path, max_pdf_pages)
    cache = {
        "schema_version": 1,
        "ticker": ticker_dir.name,
        "source_type": "concall_transcript",
        "period": metadata_row.get("period", ""),
        "created_at": utc_now(),
        "market_cap_profile": profile,
        "source": {
            "url": url,
            "pdf_path": str(pdf_path.relative_to(ROOT_DIR)),
            "sha256": pdf_hash,
            "size_bytes": pdf_path.stat().st_size,
        },
        "page_count": len(pages),
        "text_page_count": sum(1 for page in pages if page["text"]),
        "checklist_evidence": find_checklist_snippets(pages, max_snippets_per_item),
    }

    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return cache_path, cache


def read_concall_metadata(ticker_dir):
    metadata_path = ticker_dir / "concalls" / "screener_transcripts.json"
    if not metadata_path.exists():
        return []

    try:
        rows = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    return rows if isinstance(rows, list) else []


def resolve_concall_pdf(ticker_dir, row):
    file_value = row.get("file")
    if file_value:
        path = ticker_dir / file_value
        if path.exists():
            return path

    url_name = Path(urlparse(row.get("url", "")).path).name
    candidates = sorted((ticker_dir / "concalls").glob("*.pdf"))
    for candidate in candidates:
        if url_name and url_name in candidate.name:
            return candidate
    return None


def table_to_matrix(table):
    rows = []
    for tr in table.select("tr"):
        cells = [clean_text(cell.get_text(" ", strip=True)) for cell in tr.select("th,td")]
        if any(cells):
            rows.append(cells)
    return rows


def cache_screener_finance(ticker_dir, force):
    html_path = ticker_dir / "screener_finance" / "company_page.html"
    cache_path = ticker_dir / "screener_finance" / "screener_finance_cache.json"

    if not html_path.exists():
        return None, None

    html_hash = sha256_file(html_path)
    if cache_path.exists() and not force:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("source", {}).get("sha256") == html_hash:
                return cache_path, cached
        except json.JSONDecodeError:
            pass

    raw = html_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(raw, "html.parser")
    company_name_node = soup.select_one("h1")
    about_node = soup.select_one(".company-profile .sub") or soup.select_one(".about")

    tables = {}
    for table_id in SCREENER_TABLE_IDS:
        table = soup.select_one(f"#{table_id}")
        if table:
            tables[table_id] = table_to_matrix(table)

    ratios = {}
    for li in soup.select("li.flex.flex-space-between"):
        name = clean_text(li.select_one(".name").get_text(" ", strip=True)) if li.select_one(".name") else ""
        value = clean_text(li.select_one(".number").get_text(" ", strip=True)) if li.select_one(".number") else ""
        if name and value:
            ratios[name] = value

    cache = {
        "schema_version": 1,
        "ticker": ticker_dir.name,
        "source_type": "screener_finance",
        "created_at": utc_now(),
        "source": {
            "html_path": str(html_path.relative_to(ROOT_DIR)),
            "sha256": html_hash,
            "size_bytes": html_path.stat().st_size,
        },
        "company_name": clean_text(company_name_node.get_text(" ", strip=True)) if company_name_node else "",
        "about": clean_text(about_node.get_text(" ", strip=True)) if about_node else "",
        "top_ratios": ratios,
        "tables": tables,
    }

    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return cache_path, cache


def cache_news(ticker_dir):
    news_json = ticker_dir / "news" / "news_links.json"
    news_txt = ticker_dir / "news" / "news_links.txt"
    if not news_json.exists() and not news_txt.exists():
        return None

    items = []
    if news_json.exists():
        try:
            loaded = json.loads(news_json.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                items = loaded
        except json.JSONDecodeError:
            items = []

    if not items and news_txt.exists():
        items = [{"link": line.strip()} for line in news_txt.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]

    cache_dir = ticker_dir / "research_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "news_links_cache.json"
    cache = {
        "schema_version": 1,
        "ticker": ticker_dir.name,
        "source_type": "news_links",
        "created_at": utc_now(),
        "source": {
            "json_path": str(news_json.relative_to(ROOT_DIR)) if news_json.exists() else "",
            "txt_path": str(news_txt.relative_to(ROOT_DIR)) if news_txt.exists() else "",
        },
        "items": items,
    }
    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return cache_path


def fetch_annual_report_rows(session, symbol, timeout):
    url = (
        "https://www.nseindia.com/api/annual-reports"
        f"?index=equities&symbol={quote(symbol, safe='')}"
    )
    response = session.get(url, headers=NSE_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json().get("data", [])


def download_missing_annual_reports(ticker_dir, symbol, max_reports, timeout):
    session = requests.Session()
    session.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=timeout)
    rows = fetch_annual_report_rows(session, symbol, timeout)[:max_reports]
    target_dir = ticker_dir / "annual_reports"
    target_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    for row in rows:
        filename = f"{row.get('fromYr')}_{row.get('toYr')}.pdf"
        target_path = target_dir / filename
        if pdf_has_pages(target_path):
            continue

        response = session.get(row["fileName"], headers=NSE_HEADERS, timeout=timeout)
        response.raise_for_status()
        target_path.write_bytes(response.content)
        downloaded.append(str(target_path.relative_to(ROOT_DIR)))

    return downloaded


def load_registry(path):
    if not path.exists():
        return {
            "schema_version": 1,
            "created_at": utc_now(),
            "updated_at": "",
            "notes": [
                "This file is the cache registry. Original PDFs stay under data/<ticker>/annual_reports and data/<ticker>/concalls.",
                "Analysis should prefer research_cache JSON files instead of reparsing PDFs.",
            ],
            "market_cap_profiles": MARKET_CAP_PROFILES,
            "tickers": {},
        }

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise RuntimeError(f"Registry is not valid JSON: {path}")


def process_ticker(ticker_dir, cap_lookup, args):
    symbol = ticker_dir.name.upper()
    cap_info = cap_lookup.get(symbol, {})
    category = cap_info.get("category", "unknown")
    profile = MARKET_CAP_PROFILES[category]

    if args.fetch_missing_annual_reports:
        download_missing_annual_reports(ticker_dir, symbol, args.max_annual_reports, args.timeout)

    annual_reports = {}
    for pdf_path in sorted((ticker_dir / "annual_reports").glob("*.pdf")):
        try:
            cache_path, cache = cache_annual_report(
                ticker_dir=ticker_dir,
                pdf_path=pdf_path,
                profile=profile,
                max_snippets_per_item=args.max_snippets_per_item,
                max_pdf_pages=args.max_pdf_pages,
                force=args.force,
            )
            annual_reports[cache["year"]] = {
                "pdf_path": str(pdf_path.relative_to(ROOT_DIR)),
                "cache_path": str(cache_path.relative_to(ROOT_DIR)),
                "sha256": cache["source"]["sha256"],
                "extracted_at": cache["created_at"],
            }
        except Exception as exc:
            annual_reports[annual_year_from_path(pdf_path)] = {
                "pdf_path": str(pdf_path.relative_to(ROOT_DIR)),
                "error": str(exc),
            }

    concalls = []
    for row in read_concall_metadata(ticker_dir):
        pdf_path = resolve_concall_pdf(ticker_dir, row)
        if not pdf_path:
            concalls.append({"period": row.get("period", ""), "url": row.get("url", ""), "error": "PDF not found locally"})
            continue

        try:
            cache_path, cache = cache_concall(
                ticker_dir=ticker_dir,
                pdf_path=pdf_path,
                metadata_row=row,
                profile=profile,
                max_snippets_per_item=args.max_snippets_per_item,
                max_pdf_pages=args.max_pdf_pages,
                force=args.force,
            )
            concalls.append(
                {
                    "period": row.get("period", ""),
                    "url": row.get("url", ""),
                    "pdf_path": str(pdf_path.relative_to(ROOT_DIR)),
                    "cache_path": str(cache_path.relative_to(ROOT_DIR)),
                    "sha256": cache["source"]["sha256"],
                    "extracted_at": cache["created_at"],
                }
            )
        except Exception as exc:
            concalls.append(
                {
                    "period": row.get("period", ""),
                    "url": row.get("url", ""),
                    "pdf_path": str(pdf_path.relative_to(ROOT_DIR)),
                    "error": str(exc),
                }
            )

    screener_path, screener_cache = cache_screener_finance(ticker_dir, args.force)
    news_cache_path = cache_news(ticker_dir)
    screener_html_path = ticker_dir / "screener_finance" / "company_page.html"
    screener_mtime = (
        datetime.fromtimestamp(screener_html_path.stat().st_mtime, timezone.utc)
        if screener_html_path.exists()
        else None
    )
    current_quarter = quarter_key(datetime.now(timezone.utc))
    screener_quarter = quarter_key(screener_mtime) if screener_mtime else ""

    return {
        "company_name": cap_info.get("company_name", ""),
        "industry": cap_info.get("industry", ""),
        "market_cap_category": category,
        "market_cap_source": cap_info.get("source", ""),
        "rank_hint": cap_info.get("rank_hint"),
        "profile_rule": profile["rule"],
        "updated_at": utc_now(),
        "annual_reports": annual_reports,
        "concalls": concalls,
        "screener_finance": {
            "html_path": str(screener_html_path.relative_to(ROOT_DIR))
            if screener_html_path.exists()
            else "",
            "html_last_modified_at": timestamp_for_path(screener_html_path),
            "html_quarter_key": screener_quarter,
            "current_quarter_key": current_quarter,
            "needs_refresh_for_current_quarter": bool(screener_quarter and screener_quarter != current_quarter),
            "cache_path": str(screener_path.relative_to(ROOT_DIR)) if screener_path else "",
            "last_cache_built_at": screener_cache.get("created_at") if screener_cache else "",
            "html_sha256": screener_cache.get("source", {}).get("sha256") if screener_cache else "",
        },
        "news": {
            "cache_path": str(news_cache_path.relative_to(ROOT_DIR)) if news_cache_path else "",
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="Build fast JSON research caches from annual reports, concall PDFs, Screener HTML, and news links."
    )
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--config-dir", default=str(CONFIG_DIR))
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    parser.add_argument("--symbols", nargs="*", help="Ticker symbols, comma-separated or space-separated.")
    parser.add_argument("--force", action="store_true", help="Rebuild caches even when source hashes match.")
    parser.add_argument("--fetch-missing-annual-reports", action="store_true", help="Fetch NSE annual reports only when no local annual PDFs exist.")
    parser.add_argument("--max-annual-reports", type=int, default=5)
    parser.add_argument("--max-snippets-per-item", type=int, default=3)
    parser.add_argument("--max-pdf-pages", type=int, default=260, help="Maximum pages to scan per PDF cache.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--strict", action="store_true", help="Return exit code 2 when any source file cannot be cached.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    config_dir = Path(args.config_dir)
    registry_path = Path(args.registry)

    if fitz is None:
        print("ERROR: PyMuPDF/fitz is not installed; cannot extract PDF text.", file=sys.stderr)
        return 1

    selected = parse_symbols(args.symbols)
    dirs = ticker_dirs(data_dir, selected)
    if not dirs:
        print("No ticker folders matched.", file=sys.stderr)
        return 1

    registry = load_registry(registry_path)
    registry["market_cap_profiles"] = MARKET_CAP_PROFILES
    registry["updated_at"] = utc_now()
    registry.setdefault("tickers", {})

    cap_lookup = build_market_cap_lookup(config_dir)
    counts = {"tickers": 0, "annual_reports": 0, "concalls": 0, "errors": 0}

    for ticker_dir in dirs:
        print(f"Processing {ticker_dir.name}")
        ticker_record = process_ticker(ticker_dir, cap_lookup, args)
        registry["tickers"][ticker_dir.name.upper()] = ticker_record
        counts["tickers"] += 1
        counts["annual_reports"] += len(ticker_record["annual_reports"])
        counts["concalls"] += len(ticker_record["concalls"])
        counts["errors"] += sum(1 for row in ticker_record["annual_reports"].values() if row.get("error"))
        counts["errors"] += sum(1 for row in ticker_record["concalls"] if row.get("error"))

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\nDONE")
    print(json.dumps(counts, indent=2))
    print(f"Registry: {registry_path}")
    return 2 if args.strict and counts["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
