#!/usr/bin/env python3
"""
Find the best performing NSE index and print the top stocks inside it.

This script intentionally does not modify index_outperformance_ath.py. It reuses
that script's index-return helpers when it needs to discover the top index.

Examples:
    python3 scripts/top_stocks_from_top_index.py --index "NIFTY IT"
    python3 scripts/top_stocks_from_top_index.py --top-n 10 --out-csv data/top_index_stocks.csv
    python3 scripts/top_stocks_from_top_index.py --from-report-csv data/index_report.csv
    python3 scripts/top_stocks_from_top_index.py --source nifty --include-nifty-extra

Default ranking is by NSE India's perChange365d field when available.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote

import requests

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")


REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_SCANNER_PATH = Path(__file__).with_name("index_outperformance_ath.py")

NSE_BASE_URL = "https://www.nseindia.com"
NSE_INDEX_API_URL = f"{NSE_BASE_URL}/api/equity-stockIndices"
NSE_MARKET_PAGE_URL = f"{NSE_BASE_URL}/market-data/live-equity-market"
YAHOO_CHART_BASE_URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart",
    "https://query2.finance.yahoo.com/v8/finance/chart",
]

NIFTY_INDICES_BASE_URL = "https://www.niftyindices.com"
NIFTY_CONSTITUENT_BASE_URL = f"{NIFTY_INDICES_BASE_URL}/IndexConstituent"

DEFAULT_OUT_CSV = Path("data/top_stocks_from_top_index.csv")
BENCHMARK_INDICES = {"NIFTY 50", "SENSEX"}

CSV_SLUG_OVERRIDES = {
    "NIFTY 50": "ind_nifty50list.csv",
    "NIFTY NEXT 50": "ind_niftynext50list.csv",
    "NIFTY 100": "ind_nifty100list.csv",
    "NIFTY 200": "ind_nifty200list.csv",
    "NIFTY 500": "ind_nifty500list.csv",
    "NIFTY MIDCAP 50": "ind_niftymidcap50list.csv",
    "NIFTY MIDCAP 100": "ind_niftymidcap100list.csv",
    "NIFTY MIDCAP 150": "ind_niftymidcap150list.csv",
    "NIFTY SMALLCAP 50": "ind_niftysmallcap50list.csv",
    "NIFTY SMALLCAP 100": "ind_niftysmallcap100list.csv",
    "NIFTY SMALLCAP 250": "ind_niftysmallcap250list.csv",
    "NIFTY MIDSMALLCAP 400": "ind_niftymidsmallcap400list.csv",
    "NIFTY BANK": "ind_niftybanklist.csv",
    "NIFTY PRIVATE BANK": "ind_nifty_privatebanklist.csv",
    "NIFTY PSU BANK": "ind_niftypsubanklist.csv",
    "NIFTY FINANCIAL SERVICES": "ind_niftyfinancelist.csv",
    "NIFTY AUTO": "ind_niftyautolist.csv",
    "NIFTY FMCG": "ind_niftyfmcglist.csv",
    "NIFTY IT": "ind_niftyitlist.csv",
    "NIFTY MEDIA": "ind_niftymedialist.csv",
    "NIFTY METAL": "ind_niftymetallist.csv",
    "NIFTY PHARMA": "ind_niftypharmalist.csv",
    "NIFTY REALTY": "ind_niftyrealtylist.csv",
    "NIFTY HEALTHCARE": "ind_niftyhealthcarelist.csv",
    "NIFTY HEALTHCARE INDEX": "ind_niftyhealthcarelist.csv",
    "NIFTY CONSUMER DURABLES": "ind_niftyconsumerdurableslist.csv",
    "NIFTY OIL & GAS": "ind_niftyoilgaslist.csv",
    "NIFTY ENERGY": "ind_niftyenergylist.csv",
    "NIFTY INFRASTRUCTURE": "ind_niftyinfralist.csv",
    "NIFTY COMMODITIES": "ind_niftycommoditieslist.csv",
    "NIFTY INDIA CONSUMPTION": "ind_niftyconsumptionlist.csv",
    "NIFTY MNC": "ind_niftymnclist.csv",
    "NIFTY CPSE": "ind_niftycpselist.csv",
    "NIFTY PSE": "ind_niftypselist.csv",
    "NIFTY SERVICES SECTOR": "ind_niftyservicelist.csv",
}

LOCAL_CONSTITUENT_FILES = {
    "NIFTY 100": REPO_ROOT / "config/nifty100.csv",
    "NIFTY 500": REPO_ROOT / "config/ind_nifty500list.csv",
    "NIFTY SMALLCAP 250": REPO_ROOT / "config/ind_niftysmallcap250list.csv",
}


@dataclass
class StockRow:
    index_name: str
    symbol: str
    company_name: str = ""
    industry: str = ""
    last_price: float | None = None
    one_year_return_pct: float | None = None
    thirty_day_return_pct: float | None = None
    day_change_pct: float | None = None
    ffmc: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    near_year_high_pct: float | None = None
    isin: str = ""
    source: str = ""


def load_index_scanner():
    spec = importlib.util.spec_from_file_location("index_outperformance_ath", INDEX_SCANNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {INDEX_SCANNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def normalize_index_name(value: str) -> str:
    return " ".join(value.upper().replace("&AMP;", "&").split())


def parse_float(value) -> float | None:
    if value in (None, "", "-", "NA"):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def pct(value: float | None) -> str:
    return "NA" if value is None else f"{value:+.2f}%"


def number(value: float | None) -> str:
    return "NA" if value is None else f"{value:,.2f}"


def request_timeout(timeout: int) -> tuple[int, int]:
    return (min(10, timeout), timeout)


def make_nse_session(timeout: int, retries: int) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }
    )
    for attempt in range(retries + 1):
        try:
            session.get(NSE_BASE_URL, timeout=request_timeout(timeout))
            break
        except requests.RequestException:
            if attempt < retries:
                time.sleep(0.75 + attempt * 0.75)

    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Referer": NSE_MARKET_PAGE_URL,
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session


def index_api_candidates(index_name: str) -> list[str]:
    name = normalize_index_name(index_name)
    candidates = [name]
    if name.endswith(" INDEX"):
        candidates.append(name.removesuffix(" INDEX").strip())
    if "&" in name:
        candidates.append(name.replace("&", "%26"))
    return list(dict.fromkeys(candidates))


def fetch_nse_index_constituents(
    index_name: str,
    timeout: int,
    retries: int,
) -> tuple[str, list[StockRow]]:
    session = make_nse_session(timeout=timeout, retries=retries)
    errors = []
    for candidate in index_api_candidates(index_name):
        for attempt in range(retries + 1):
            try:
                response = session.get(
                    NSE_INDEX_API_URL,
                    params={"index": candidate},
                    timeout=request_timeout(timeout),
                )
                response.raise_for_status()
                payload = response.json()
                rows = stock_rows_from_nse_payload(index_name, payload)
                if rows:
                    return candidate, rows
                errors.append(f"{candidate}: no stock rows returned")
                break
            except (requests.RequestException, ValueError) as exc:
                if attempt >= retries:
                    errors.append(f"{candidate}: {exc}")
                else:
                    time.sleep(0.75 + attempt * 0.75)
    raise RuntimeError("; ".join(errors) or f"No NSE data for {index_name}")


def stock_rows_from_nse_payload(index_name: str, payload: dict) -> list[StockRow]:
    raw_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_rows, list):
        return []

    rows: list[StockRow] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol == normalize_index_name(index_name):
            continue
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        company_name = (
            item.get("companyName")
            or meta.get("companyName")
            or item.get("identifier")
            or symbol
        )
        rows.append(
            StockRow(
                index_name=normalize_index_name(index_name),
                symbol=symbol,
                company_name=str(company_name),
                industry=str(item.get("industry") or meta.get("industry") or ""),
                last_price=parse_float(item.get("lastPrice")),
                one_year_return_pct=parse_float(item.get("perChange365d")),
                thirty_day_return_pct=parse_float(item.get("perChange30d")),
                day_change_pct=parse_float(item.get("pChange")),
                ffmc=parse_float(item.get("ffmc")),
                year_high=parse_float(item.get("yearHigh")),
                year_low=parse_float(item.get("yearLow")),
                near_year_high_pct=parse_float(item.get("nearWKH")),
                isin=str(item.get("isin") or meta.get("isin") or ""),
                source="nse_api",
            )
        )
    return rows


def csv_filename_for_index(index_name: str) -> str:
    name = normalize_index_name(index_name)
    if name in CSV_SLUG_OVERRIDES:
        return CSV_SLUG_OVERRIDES[name]
    compact = "".join(ch for ch in name.lower() if ch.isalnum())
    if compact.startswith("nifty"):
        return f"ind_{compact}list.csv"
    return f"ind_{compact}list.csv"


def csv_urls_for_index(index_name: str) -> list[str]:
    filename = csv_filename_for_index(index_name)
    return [
        f"{NIFTY_CONSTITUENT_BASE_URL}/{filename}",
        f"{NIFTY_CONSTITUENT_BASE_URL}/{quote(filename)}",
    ]


def read_constituents_csv(index_name: str, path: Path) -> list[StockRow]:
    rows: list[StockRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            normalized = {str(k).strip().lower(): v for k, v in row.items()}
            symbol = (
                normalized.get("symbol")
                or normalized.get("nse symbol")
                or normalized.get("ticker")
                or ""
            )
            symbol = str(symbol).strip().upper()
            if not symbol:
                continue
            rows.append(
                StockRow(
                    index_name=normalize_index_name(index_name),
                    symbol=symbol,
                    company_name=str(
                        normalized.get("company name")
                        or normalized.get("company")
                        or normalized.get("name")
                        or symbol
                    ),
                    industry=str(normalized.get("industry") or normalized.get("sector") or ""),
                    isin=str(normalized.get("isin code") or normalized.get("isin") or ""),
                    source=f"csv:{path}",
                )
            )
    return rows


def download_constituents_csv(index_name: str, timeout: int, retries: int) -> list[StockRow]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Accept": "text/csv,text/plain,*/*",
            "Referer": NIFTY_INDICES_BASE_URL,
        }
    )
    errors = []
    for url in csv_urls_for_index(index_name):
        for attempt in range(retries + 1):
            try:
                response = session.get(url, timeout=request_timeout(timeout))
                response.raise_for_status()
                text = response.text.lstrip("\ufeff")
                if "Company Name" not in text[:300] or "Symbol" not in text[:300]:
                    raise ValueError("response does not look like a constituent CSV")
                temp_path = REPO_ROOT / "tmp" / csv_filename_for_index(index_name)
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path.write_text(text, encoding="utf-8")
                return read_constituents_csv(index_name, temp_path)
            except (requests.RequestException, ValueError) as exc:
                if attempt >= retries:
                    errors.append(f"{url}: {exc}")
                else:
                    time.sleep(0.75 + attempt * 0.75)
    raise RuntimeError("; ".join(errors))


def fetch_csv_constituents(index_name: str, timeout: int, retries: int) -> list[StockRow]:
    local_path = LOCAL_CONSTITUENT_FILES.get(normalize_index_name(index_name))
    if local_path and local_path.exists():
        return read_constituents_csv(index_name, local_path)
    return download_constituents_csv(index_name, timeout=timeout, retries=retries)


def fetch_constituents(
    index_name: str,
    source: str,
    timeout: int,
    retries: int,
) -> tuple[str, list[StockRow]]:
    if source in ("auto", "nse"):
        try:
            matched_name, rows = fetch_nse_index_constituents(
                index_name=index_name,
                timeout=timeout,
                retries=retries,
            )
            return f"nse_api:{matched_name}", rows
        except RuntimeError:
            if source == "nse":
                raise

    rows = fetch_csv_constituents(index_name, timeout=timeout, retries=retries)
    return "constituent_csv", rows


def yahoo_symbol(symbol: str) -> str:
    return f"{symbol}.NS"


def yahoo_history_summary(symbol: str, timeout: int) -> dict[str, float | None]:
    params = {"range": "1y", "interval": "1d"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
    }
    last_error = None
    for base_url in YAHOO_CHART_BASE_URLS:
        try:
            response = requests.get(
                f"{base_url}/{quote(yahoo_symbol(symbol), safe='')}",
                params=params,
                headers=headers,
                timeout=request_timeout(timeout),
            )
            if response.status_code == 429:
                last_error = RuntimeError("Yahoo Finance rate limited the request")
                continue
            response.raise_for_status()
            chart = response.json().get("chart", {})
            if chart.get("error"):
                raise RuntimeError(str(chart["error"]))
            result = (chart.get("result") or [None])[0]
            if not result:
                raise RuntimeError("empty Yahoo chart result")
            quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
            closes = [value for value in (quote_data.get("close") or []) if value is not None]
            highs = [value for value in (quote_data.get("high") or []) if value is not None]
            lows = [value for value in (quote_data.get("low") or []) if value is not None]
            if len(closes) < 2:
                raise RuntimeError("not enough Yahoo close prices")
            latest_close = float(closes[-1])
            first_close = float(closes[0])
            one_year_return = ((latest_close / first_close) - 1.0) * 100.0
            year_high = max(float(value) for value in highs) if highs else None
            year_low = min(float(value) for value in lows) if lows else None
            near_high = None
            if year_high and year_high > 0:
                near_high = ((latest_close / year_high) - 1.0) * 100.0
            return {
                "last_price": latest_close,
                "one_year_return_pct": one_year_return,
                "year_high": year_high,
                "year_low": year_low,
                "near_year_high_pct": near_high,
            }
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
    raise RuntimeError(str(last_error) if last_error else "Yahoo Finance request failed")


def enrich_rows_with_yahoo(
    rows: Sequence[StockRow],
    timeout: int,
    sleep_seconds: float,
    max_symbols: int,
) -> None:
    if max_symbols <= 0:
        return
    for offset, row in enumerate(rows[:max_symbols], start=1):
        if row.one_year_return_pct is not None:
            continue
        try:
            summary = yahoo_history_summary(row.symbol, timeout=timeout)
            row.last_price = summary["last_price"]
            row.one_year_return_pct = summary["one_year_return_pct"]
            row.year_high = summary["year_high"]
            row.year_low = summary["year_low"]
            row.near_year_high_pct = summary["near_year_high_pct"]
            row.source = f"{row.source}+yahoo_chart"
            print(f"YF  {offset}/{min(len(rows), max_symbols)} {row.symbol}: {pct(row.one_year_return_pct)}")
        except RuntimeError as exc:
            print(f"YF  {offset}/{min(len(rows), max_symbols)} {row.symbol}: {exc}", file=sys.stderr)
        time.sleep(sleep_seconds)


def rank_key(row: StockRow, rank_by: str) -> float:
    values = {
        "1y-return": row.one_year_return_pct,
        "30d-return": row.thirty_day_return_pct,
        "today-change": row.day_change_pct,
        "free-float-mcap": row.ffmc,
        "last-price": row.last_price,
        "near-year-high": None if row.near_year_high_pct is None else -row.near_year_high_pct,
        "symbol": None,
    }
    value = values[rank_by]
    if value is None:
        return float("-inf")
    return float(value)


def sort_stocks(rows: Sequence[StockRow], rank_by: str) -> list[StockRow]:
    if rank_by == "symbol":
        return sorted(rows, key=lambda row: row.symbol)
    if all(rank_key(row, rank_by) == float("-inf") for row in rows):
        return sorted(rows, key=lambda row: row.symbol)
    return sorted(
        rows,
        key=lambda row: (rank_key(row, rank_by), row.symbol),
        reverse=True,
    )


def pick_top_index_from_report(path: Path, exclude_benchmarks: bool) -> tuple[str, float | None]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        candidates = []
        for row in reader:
            index_name = normalize_index_name(row.get("index") or row.get("index_name") or "")
            if not index_name:
                continue
            if exclude_benchmarks and index_name in BENCHMARK_INDICES:
                continue
            value = parse_float(row.get("one_year_return_pct") or row.get("return_pct"))
            if value is None:
                continue
            candidates.append((value, index_name))
    if not candidates:
        raise RuntimeError(f"No usable index return rows found in {path}")
    value, index_name = max(candidates)
    return index_name, value


def discover_top_index(args) -> tuple[str, float | None]:
    scanner = load_index_scanner()
    as_of = args.as_of
    one_year_start = as_of - timedelta(days=args.one_year_days)
    indices = scanner.load_indices(args.indices_file)
    if args.include_nifty_extra and not args.indices_file:
        indices = scanner.NIFTY_INDICES_EXTRA

    if args.index_source == "nifty":
        client = scanner.NiftyIndicesClient(
            timeout=args.timeout,
            sleep_seconds=args.sleep_seconds,
        )
    else:
        client = scanner.YahooIndexClient(
            scanner.YAHOO_INDEX_SYMBOLS,
            timeout=args.timeout,
            sleep_seconds=args.sleep_seconds,
        )

    candidates = []
    print(
        f"Discovering top index across {len(indices)} indices "
        f"from {one_year_start} to {as_of} using {args.index_source}..."
    )
    for name in indices:
        normalized_name = normalize_index_name(name)
        if args.exclude_benchmarks and normalized_name in BENCHMARK_INDICES:
            continue
        try:
            if args.index_source == "yahoo":
                ath_rows = client.history(name, args.ath_start, as_of)
                one_year_rows = scanner.filter_rows_by_date(ath_rows, one_year_start, as_of)
            else:
                one_year_rows = client.history(name, one_year_start, as_of)
                ath_rows = client.history(name, args.ath_start, as_of)
            report = scanner.make_report(
                name,
                one_year_rows,
                ath_rows,
                args.ath_threshold_pct,
            )
            if report.one_year_return_pct is not None:
                candidates.append((report.one_year_return_pct, normalized_name))
                print(f"OK  {normalized_name}: {pct(report.one_year_return_pct)}")
        except Exception as exc:
            print(f"ERR {normalized_name}: {exc}", file=sys.stderr)

    if not candidates:
        raise RuntimeError("Could not discover a top index")
    value, index_name = max(candidates)
    return index_name, value


def choose_index(args) -> tuple[str, float | None]:
    if args.index:
        return normalize_index_name(args.index), None
    if args.from_report_csv:
        return pick_top_index_from_report(
            Path(args.from_report_csv),
            exclude_benchmarks=args.exclude_benchmarks,
        )
    return discover_top_index(args)


def print_stock_table(index_name: str, rows: Sequence[StockRow], rank_by: str) -> None:
    print(f"\nTop {len(rows)} stocks in {index_name} by {rank_by}")
    print("-" * (len(index_name) + len(rank_by) + 19))
    if not rows:
        print("No stocks found.")
        return

    table = []
    for rank, row in enumerate(rows, start=1):
        table.append(
            [
                str(rank),
                row.symbol,
                row.company_name,
                row.industry,
                pct(row.one_year_return_pct),
                pct(row.thirty_day_return_pct),
                pct(row.day_change_pct),
                number(row.last_price),
            ]
        )
    headers = ["#", "Symbol", "Company", "Industry", "1Y", "30D", "Day", "Last"]
    widths = [
        max(len(headers[i]), *(len(item[i]) for item in table))
        for i in range(len(headers))
    ]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("  ".join("-" * width for width in widths))
    for item in table:
        print("  ".join(item[i].ljust(widths[i]) for i in range(len(item))))


def write_output_csv(path: Path, rows: Sequence[StockRow], rank_by: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "rank",
            "rank_by",
            "index_name",
            "symbol",
            "company_name",
            "industry",
            "last_price",
            "one_year_return_pct",
            "thirty_day_return_pct",
            "day_change_pct",
            "ffmc",
            "year_high",
            "year_low",
            "near_year_high_pct",
            "isin",
            "source",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for rank, row in enumerate(rows, start=1):
            writer.writerow(
                {
                    "rank": rank,
                    "rank_by": rank_by,
                    "index_name": row.index_name,
                    "symbol": row.symbol,
                    "company_name": row.company_name,
                    "industry": row.industry,
                    "last_price": row.last_price,
                    "one_year_return_pct": row.one_year_return_pct,
                    "thirty_day_return_pct": row.thirty_day_return_pct,
                    "day_change_pct": row.day_change_pct,
                    "ffmc": row.ffmc,
                    "year_high": row.year_high,
                    "year_low": row.year_low,
                    "near_year_high_pct": row.near_year_high_pct,
                    "isin": row.isin,
                    "source": row.source,
                }
            )


def parse_ymd(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Get top stocks from the top performing NSE index"
    )
    parser.add_argument("--index", help="Skip discovery and fetch this index directly")
    parser.add_argument(
        "--from-report-csv",
        help="CSV produced by scripts/index_outperformance_ath.py --out-csv",
    )
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument(
        "--rank-by",
        choices=(
            "1y-return",
            "30d-return",
            "today-change",
            "free-float-mcap",
            "last-price",
            "near-year-high",
            "symbol",
        ),
        default="1y-return",
    )
    parser.add_argument(
        "--constituents-source",
        choices=("auto", "nse", "csv"),
        default="auto",
        help="auto uses NSE API first, then NiftyIndices/local CSV fallback",
    )
    parser.add_argument(
        "--out-csv",
        default=str(DEFAULT_OUT_CSV),
        help="Output CSV path",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument(
        "--no-yahoo-enrich",
        action="store_true",
        help="Do not use Yahoo chart data to rank CSV-only constituent rows",
    )
    parser.add_argument(
        "--max-yahoo-symbols",
        type=int,
        default=250,
        help="Maximum CSV constituent rows to enrich via Yahoo chart fallback",
    )
    parser.add_argument("--as-of", type=parse_ymd, default=date.today())
    parser.add_argument("--one-year-days", type=int, default=365)
    parser.add_argument("--ath-start", type=parse_ymd, default=date(2000, 1, 1))
    parser.add_argument("--ath-threshold-pct", type=float, default=0.25)
    parser.add_argument("--indices-file")
    parser.add_argument(
        "--index-source",
        choices=("yahoo", "nifty"),
        default="yahoo",
        help="Source used only while discovering the top index",
    )
    parser.add_argument(
        "--include-nifty-extra",
        action="store_true",
        help="With --index-source nifty, scan the larger built-in index list",
    )
    parser.add_argument(
        "--include-benchmarks",
        dest="exclude_benchmarks",
        action="store_false",
        help="Allow NIFTY 50/SENSEX to be selected as top index",
    )
    parser.set_defaults(exclude_benchmarks=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    index_name, index_return = choose_index(args)
    if index_return is None:
        print(f"Selected index: {index_name}")
    else:
        print(f"Selected index: {index_name} ({pct(index_return)} 1Y)")

    source, constituents = fetch_constituents(
        index_name=index_name,
        source=args.constituents_source,
        timeout=args.timeout,
        retries=args.retries,
    )
    if (
        not args.no_yahoo_enrich
        and args.rank_by in {"1y-return", "last-price", "near-year-high"}
        and any(row.one_year_return_pct is None for row in constituents)
    ):
        print("Enriching CSV constituent rows with Yahoo chart performance...")
        enrich_rows_with_yahoo(
            constituents,
            timeout=args.timeout,
            sleep_seconds=args.sleep_seconds,
            max_symbols=args.max_yahoo_symbols,
        )
    ranked = sort_stocks(constituents, args.rank_by)
    top_rows = ranked[: args.top_n]

    print(f"Constituent source: {source}")
    if args.rank_by != "symbol" and all(rank_key(row, args.rank_by) == float("-inf") for row in ranked):
        print(
            f"Warning: {args.rank_by} was unavailable for all rows. "
            "Use --constituents-source nse for live NSE performance fields.",
            file=sys.stderr,
        )

    print_stock_table(index_name, top_rows, args.rank_by)
    out_csv = Path(args.out_csv)
    write_output_csv(out_csv, top_rows, args.rank_by)
    print(f"\nCSV saved: {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
