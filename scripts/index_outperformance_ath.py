#!/usr/bin/env python3
"""
Find NSE indices that beat NIFTY 50 and SENSEX over the last year, and indices
that are trading near/touching their all-time high.

Run:
    venv/bin/python scripts/index_outperformance_ath.py

Optional:
    venv/bin/python scripts/index_outperformance_ath.py --out-csv data/index_report.csv
    venv/bin/python scripts/index_outperformance_ath.py --indices-file my_indices.txt
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.parse import quote, urljoin

import requests


YAHOO_INDEX_SYMBOLS = {
    "NIFTY 50": "^NSEI",
    "NIFTY MIDCAP 50": "^NSEMDCP50",
    "NIFTY MIDCAP 100": "^NSMIDCP",
    "NIFTY BANK": "^NSEBANK",
    "NIFTY AUTO": "^CNXAUTO",
    "NIFTY ENERGY": "^CNXENERGY",
    "NIFTY FMCG": "^CNXFMCG",
    "NIFTY IT": "^CNXIT",
    "NIFTY MEDIA": "^CNXMEDIA",
    "NIFTY METAL": "^CNXMETAL",
    "NIFTY PHARMA": "^CNXPHARMA",
    "NIFTY PSU BANK": "^CNXPSUBANK",
    "NIFTY REALTY": "^CNXREALTY",
    "NIFTY INFRASTRUCTURE": "^CNXINFRA",
    "NIFTY MNC": "^CNXMNC",
    "NIFTY PSE": "^CNXPSE",
    "NIFTY SERVICES SECTOR": "^CNXSERVICE",
}

NIFTY_INDICES_EXTRA = [
    "NIFTY 50",
    "NIFTY NEXT 50",
    "NIFTY 100",
    "NIFTY 200",
    "NIFTY 500",
    "NIFTY MIDCAP 50",
    "NIFTY MIDCAP 100",
    "NIFTY MIDCAP 150",
    "NIFTY SMALLCAP 50",
    "NIFTY SMALLCAP 100",
    "NIFTY SMALLCAP 250",
    "NIFTY MIDSMALLCAP 400",
    "NIFTY BANK",
    "NIFTY PRIVATE BANK",
    "NIFTY PSU BANK",
    "NIFTY FINANCIAL SERVICES",
    "NIFTY AUTO",
    "NIFTY FMCG",
    "NIFTY IT",
    "NIFTY MEDIA",
    "NIFTY METAL",
    "NIFTY PHARMA",
    "NIFTY REALTY",
    "NIFTY HEALTHCARE INDEX",
    "NIFTY CONSUMER DURABLES",
    "NIFTY OIL & GAS",
    "NIFTY ENERGY",
    "NIFTY INFRASTRUCTURE",
    "NIFTY COMMODITIES",
    "NIFTY INDIA CONSUMPTION",
    "NIFTY MNC",
    "NIFTY CPSE",
    "NIFTY PSE",
    "NIFTY SERVICES SECTOR",
]

DEFAULT_INDICES = list(YAHOO_INDEX_SYMBOLS)


@dataclass
class IndexReport:
    name: str
    one_year_return_pct: float | None
    start_date: date | None
    start_close: float | None
    latest_date: date | None
    latest_close: float | None
    latest_high: float | None
    ath_high: float | None
    ath_high_date: date | None
    distance_from_ath_pct: float | None
    touching_ath: bool
    error: str = ""


class NiftyIndicesClient:
    """Small standalone version of Code 2's NiftyIndices historical fetcher."""

    base_url = "https://niftyindices.com"
    path = "/Backpage.aspx/getHistoricaldatatabletoString"

    def __init__(self, timeout: int = 30, sleep_seconds: float = 0.35):
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Host": "niftyindices.com",
                "Referer": "https://niftyindices.com",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/144.0.0.0 Safari/537.36"
                ),
                "Origin": "https://niftyindices.com",
                "Accept": "*/*",
                "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Content-Type": "application/json; charset=UTF-8",
            }
        )

    def history(self, name: str, from_date: date, to_date: date) -> list[dict]:
        rows: list[dict] = []
        for start, end in split_date_ranges(from_date, to_date, days=365):
            payload = {
                "name": name,
                "startDate": start.strftime("%d-%b-%Y"),
                "endDate": end.strftime("%d-%b-%Y"),
            }
            url = urljoin(self.base_url, self.path)
            response = self.session.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
            chunk = json.loads(body["d"])
            if isinstance(chunk, list):
                rows.extend(chunk)
            time.sleep(self.sleep_seconds)
        return sorted(rows, key=row_date)


class YahooIndexClient:
    """Yahoo Finance fallback for mapped NSE/BSE index symbols."""

    base_urls = [
        "https://query1.finance.yahoo.com/v8/finance/chart",
        "https://query2.finance.yahoo.com/v8/finance/chart",
    ]

    def __init__(self, symbols: dict[str, str], timeout: int = 30, sleep_seconds: float = 1.0):
        self.symbols = symbols
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
            }
        )

    def history(self, name: str, from_date: date, to_date: date) -> list[dict]:
        symbol = self.symbols.get(name)
        if not symbol:
            raise ValueError(f"No Yahoo symbol mapping for {name!r}. Use --source nifty for this index.")
        params = {"range": "max", "interval": "1d"}
        response = None
        for attempt in range(3):
            for base_url in self.base_urls:
                url = f"{base_url}/{quote(symbol, safe='')}"
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code != 429:
                    break
            if response is not None and response.status_code != 429:
                break
            time.sleep(2 * (attempt + 1))
        if response is None:
            raise ValueError("No response from Yahoo Finance")
        response.raise_for_status()
        chart = response.json().get("chart", {})
        if chart.get("error"):
            raise ValueError(chart["error"])
        result = (chart.get("result") or [None])[0]
        if not result:
            raise ValueError("Yahoo Finance returned no data")

        timestamps = result.get("timestamp") or []
        quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote_data.get("close") or []
        highs = quote_data.get("high") or []
        rows = []
        for ts, close, high in zip(timestamps, closes, highs):
            if close is None:
                continue
            dt = datetime.fromtimestamp(ts).date()
            rows.append(
                {
                    "HistoricalDate": dt.isoformat(),
                    "CLOSE": close,
                    "HIGH": high if high is not None else close,
                }
            )
        time.sleep(self.sleep_seconds)
        return filter_rows_by_date(rows, from_date, to_date)


def split_date_ranges(from_date: date, to_date: date, days: int) -> Iterable[tuple[date, date]]:
    cursor = from_date
    while cursor <= to_date:
        end = min(cursor + timedelta(days=days - 1), to_date)
        yield cursor, end
        cursor = end + timedelta(days=1)


def parse_date(value: object) -> date:
    text = str(value).strip()
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unknown date format: {value!r}")


def row_date(row: dict) -> date:
    return parse_date(row.get("HistoricalDate") or row.get("DATE") or row.get("Date"))


def row_float(row: dict, *keys: str) -> float | None:
    for key in keys:
        if key not in row:
            continue
        value = row[key]
        if value in ("", "-", None):
            continue
        try:
            return float(str(value).replace(",", ""))
        except ValueError:
            continue
    return None


def clean_rows(rows: list[dict]) -> list[dict]:
    cleaned = []
    seen = set()
    for row in rows:
        try:
            dt = row_date(row)
            close = row_float(row, "CLOSE", "Close", "close")
        except ValueError:
            continue
        if close is None or math.isnan(close):
            continue
        key = (dt, close)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(row)
    return sorted(cleaned, key=row_date)


def filter_rows_by_date(rows: list[dict], from_date: date, to_date: date) -> list[dict]:
    filtered = []
    for row in rows:
        try:
            dt = row_date(row)
        except ValueError:
            continue
        if from_date <= dt <= to_date:
            filtered.append(row)
    return sorted(filtered, key=row_date)


def make_report(name: str, one_year_rows: list[dict], ath_rows: list[dict], ath_threshold_pct: float) -> IndexReport:
    one_year_rows = clean_rows(one_year_rows)
    ath_rows = clean_rows(ath_rows)
    if len(one_year_rows) < 2:
        raise ValueError("Not enough one-year data")
    if not ath_rows:
        raise ValueError("Not enough ATH data")

    start_row = one_year_rows[0]
    latest_row = one_year_rows[-1]
    start_close = row_float(start_row, "CLOSE", "Close", "close")
    latest_close = row_float(latest_row, "CLOSE", "Close", "close")
    latest_high = row_float(latest_row, "HIGH", "High", "high") or latest_close
    if not start_close or not latest_close:
        raise ValueError("Missing close price")

    high_rows = []
    for row in ath_rows:
        high = row_float(row, "HIGH", "High", "high") or row_float(row, "CLOSE", "Close", "close")
        if high is not None:
            high_rows.append((high, row))
    if not high_rows:
        raise ValueError("Missing high price")

    ath_high, ath_row = max(high_rows, key=lambda item: item[0])
    distance_from_ath_pct = ((latest_high / ath_high) - 1.0) * 100.0
    return IndexReport(
        name=name,
        one_year_return_pct=((latest_close / start_close) - 1.0) * 100.0,
        start_date=row_date(start_row),
        start_close=start_close,
        latest_date=row_date(latest_row),
        latest_close=latest_close,
        latest_high=latest_high,
        ath_high=ath_high,
        ath_high_date=row_date(ath_row),
        distance_from_ath_pct=distance_from_ath_pct,
        touching_ath=distance_from_ath_pct >= -ath_threshold_pct,
    )


def fetch_sensex_return(from_date: date, to_date: date, timeout: int) -> tuple[float, date, date]:
    client = YahooIndexClient({"SENSEX": "^BSESN"}, timeout=timeout, sleep_seconds=0)
    rows = clean_rows(client.history("SENSEX", from_date, to_date))
    points = [(row_date(row), row_float(row, "CLOSE")) for row in rows]
    points = [(dt, close) for dt, close in points if close is not None]
    if len(points) < 2:
        raise ValueError("Not enough SENSEX data from Yahoo Finance")
    start_dt, start_close = points[0]
    end_dt, end_close = points[-1]
    return ((end_close / start_close) - 1.0) * 100.0, start_dt, end_dt


def load_indices(path: str | None) -> list[str]:
    if not path:
        return DEFAULT_INDICES
    with open(path, "r", encoding="utf-8") as fp:
        return [line.strip() for line in fp if line.strip() and not line.startswith("#")]


def pct(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value:,.2f}%"


def number(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value:,.2f}"


def print_table(title: str, reports: list[IndexReport], columns: list[str]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    if not reports:
        print("No indices found.")
        return
    headers = {
        "name": "Index",
        "return": "1Y Return",
        "latest": "Latest Close",
        "ath": "ATH High",
        "distance": "From ATH",
        "latest_date": "Latest Date",
    }
    rows = []
    for report in reports:
        values = {
            "name": report.name,
            "return": pct(report.one_year_return_pct),
            "latest": number(report.latest_close),
            "ath": number(report.ath_high),
            "distance": pct(report.distance_from_ath_pct),
            "latest_date": report.latest_date.isoformat() if report.latest_date else "NA",
        }
        rows.append([values[col] for col in columns])
    widths = [
        max(len(headers[col]), *(len(row[i]) for row in rows))
        for i, col in enumerate(columns)
    ]
    print("  ".join(headers[col].ljust(widths[i]) for i, col in enumerate(columns)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(columns))))


def write_csv(path: str, reports: list[IndexReport]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "index",
                "one_year_return_pct",
                "start_date",
                "start_close",
                "latest_date",
                "latest_close",
                "latest_high",
                "ath_high",
                "ath_high_date",
                "distance_from_ath_pct",
                "touching_ath",
                "error",
            ],
        )
        writer.writeheader()
        for report in reports:
            writer.writerow(
                {
                    "index": report.name,
                    "one_year_return_pct": report.one_year_return_pct,
                    "start_date": report.start_date,
                    "start_close": report.start_close,
                    "latest_date": report.latest_date,
                    "latest_close": report.latest_close,
                    "latest_high": report.latest_high,
                    "ath_high": report.ath_high,
                    "ath_high_date": report.ath_high_date,
                    "distance_from_ath_pct": report.distance_from_ath_pct,
                    "touching_ath": report.touching_ath,
                    "error": report.error,
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="NSE index outperformance and ATH scanner")
    parser.add_argument("--indices-file", help="Text file with one NiftyIndices index name per line")
    parser.add_argument("--out-csv", help="Optional CSV output path")
    parser.add_argument("--as-of", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(), default=date.today())
    parser.add_argument("--one-year-days", type=int, default=365)
    parser.add_argument("--ath-start", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(), default=date(2000, 1, 1))
    parser.add_argument("--ath-threshold-pct", type=float, default=0.25, help="Treat as ATH touch if latest high is within this percent of ATH")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--sleep-seconds", type=float, default=1.0, help="Pause between index requests")
    parser.add_argument(
        "--source",
        choices=("yahoo", "nifty"),
        default="yahoo",
        help="yahoo is more reliable for mapped indices; nifty uses the NiftyIndices API like Code 2",
    )
    parser.add_argument(
        "--include-nifty-extra",
        action="store_true",
        help="With --source nifty, scan the larger built-in NiftyIndices list",
    )
    args = parser.parse_args()

    one_year_start = args.as_of - timedelta(days=args.one_year_days)
    indices = load_indices(args.indices_file)
    if args.include_nifty_extra and not args.indices_file:
        indices = NIFTY_INDICES_EXTRA
    if args.source == "nifty":
        client = NiftyIndicesClient(timeout=args.timeout, sleep_seconds=args.sleep_seconds)
    else:
        client = YahooIndexClient(YAHOO_INDEX_SYMBOLS, timeout=args.timeout, sleep_seconds=args.sleep_seconds)
    reports: list[IndexReport] = []

    print(f"Scanning {len(indices)} indices from {one_year_start} to {args.as_of} using {args.source}...")
    for name in indices:
        try:
            if args.source == "yahoo":
                ath_rows = client.history(name, args.ath_start, args.as_of)
                one_year_rows = filter_rows_by_date(ath_rows, one_year_start, args.as_of)
            else:
                one_year_rows = client.history(name, one_year_start, args.as_of)
                ath_rows = client.history(name, args.ath_start, args.as_of)
            report = make_report(name, one_year_rows, ath_rows, args.ath_threshold_pct)
            reports.append(report)
            print(f"OK  {name}: {pct(report.one_year_return_pct)}, ATH distance {pct(report.distance_from_ath_pct)}")
        except Exception as exc:
            reports.append(
                IndexReport(
                    name=name,
                    one_year_return_pct=None,
                    start_date=None,
                    start_close=None,
                    latest_date=None,
                    latest_close=None,
                    latest_high=None,
                    ath_high=None,
                    ath_high_date=None,
                    distance_from_ath_pct=None,
                    touching_ath=False,
                    error=str(exc),
                )
            )
            print(f"ERR {name}: {exc}", file=sys.stderr)

    nifty50 = next((item for item in reports if item.name == "NIFTY 50" and item.one_year_return_pct is not None), None)
    if not nifty50:
        print("\nNIFTY 50 benchmark missing, cannot calculate outperformance.", file=sys.stderr)
        return 1

    try:
        sensex_return, sensex_start, sensex_end = fetch_sensex_return(one_year_start, args.as_of, args.timeout)
        sensex_note = f"SENSEX: {pct(sensex_return)} ({sensex_start} to {sensex_end})"
    except Exception as exc:
        sensex_return = None
        sensex_note = f"SENSEX unavailable: {exc}"

    benchmark_return = nifty50.one_year_return_pct
    if sensex_return is not None:
        benchmark_return = max(nifty50.one_year_return_pct, sensex_return)

    valid_reports = [item for item in reports if item.one_year_return_pct is not None]
    outperformers = sorted(
        [item for item in valid_reports if item.one_year_return_pct and item.one_year_return_pct > benchmark_return],
        key=lambda item: item.one_year_return_pct or -999,
        reverse=True,
    )
    ath_touching = sorted(
        [item for item in valid_reports if item.touching_ath],
        key=lambda item: item.distance_from_ath_pct or -999,
        reverse=True,
    )

    print("\nBenchmarks")
    print("----------")
    print(f"NIFTY 50: {pct(nifty50.one_year_return_pct)} ({nifty50.start_date} to {nifty50.latest_date})")
    print(sensex_note)
    print(f"Outperformance cutoff used: {pct(benchmark_return)}")

    print_table(
        "Indices Beating Both NIFTY 50 And SENSEX",
        outperformers,
        ["name", "return", "latest", "latest_date"],
    )
    print_table(
        f"Indices Touching ATH (within {args.ath_threshold_pct}% of ATH high)",
        ath_touching,
        ["name", "latest", "ath", "distance", "latest_date"],
    )

    failed = [item for item in reports if item.error]
    if failed:
        print_table("Failed / Unsupported Index Names", failed, ["name"])

    if args.out_csv:
        write_csv(args.out_csv, reports)
        print(f"\nCSV saved: {args.out_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
