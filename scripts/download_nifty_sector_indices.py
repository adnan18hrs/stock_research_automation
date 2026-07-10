#!/usr/bin/env python3
"""
Discover Nifty sector-like indices and screen them against NIFTY 50.

Examples:
    python3 scripts/download_nifty_sector_indices.py --list-only
    python3 scripts/download_nifty_sector_indices.py --years 1
    python3 scripts/download_nifty_sector_indices.py --indices "NIFTY IT" "NIFTY BANK" "NIFTY FMCG" "NIFTY ENERGY"

Default output is a text file of passing index names, not price-history CSVs.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

import requests


NIFTY_BASE_URL = "https://www.niftyindices.com"
NIFTY_INDEX_MAPPING_URL = (
    "https://liveindexsa.niftyindices.com/assets/json/IndexMapping.json"
)
NIFTY_HISTORY_URL = f"{NIFTY_BASE_URL}/BackPage/getHistoricaldatatabletoString"
NIFTY_HISTORY_PAGE_URL = f"{NIFTY_BASE_URL}/reports/historical-data"

MAX_HISTORY_DAYS = 366

DEFAULT_OUT_DIR = Path("data/nifty_indices")
DEFAULT_PASSED_FILE = DEFAULT_OUT_DIR / "passed_sector_indices.txt"
BENCHMARK_INDEX = "NIFTY 50"

DEFAULT_SECTOR_INDICES = [
    "NIFTY AUTO",
    "NIFTY BANK",
    "NIFTY CAPITAL GOODS",
    "NIFTY CEMENT",
    "NIFTY CHEMICALS",
    "NIFTY COMMERCIAL & TRANSPORT SERVICES",
    "NIFTY CONSTRUCTION",
    "NIFTY CONSUMER DURABLES",
    "NIFTY CONSUMER SERVICES",
    "NIFTY ENERGY",
    "NIFTY FINANCIAL SERVICES",
    "NIFTY FINANCIAL SERVICES 25/50",
    "NIFTY FINANCIAL SERVICES EX-BANK",
    "NIFTY FMCG",
    "NIFTY HEALTHCARE",
    "NIFTY HOSPITALS",
    "NIFTY HOUSING FINANCE",
    "NIFTY INSURANCE",
    "NIFTY IT",
    "NIFTY MEDIA",
    "NIFTY METAL",
    "NIFTY MIDSMALL FINANCIAL SERVICES",
    "NIFTY MIDSMALL HEALTHCARE",
    "NIFTY MIDSMALL IT & TELECOM",
    "NIFTY NBFC",
    "NIFTY OIL & GAS",
    "NIFTY PHARMA",
    "NIFTY POWER",
    "NIFTY PRIVATE BANK",
    "NIFTY PSU BANK",
    "NIFTY REALTY",
    "NIFTY REITS & REALTY",
    "NIFTY RETAIL",
    "NIFTY TELECOMMUNICATIONS",
    "NIFTY500 HEALTHCARE",
]

HISTORY_COLUMNS = [
    "index_name",
    "date",
    "open",
    "high",
    "low",
    "close",
    "pe",
    "pb",
    "div_yield",
    "total_returns_index",
]


def make_nifty_indices_session(timeout: int, retries: int) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-IN,en-US;q=0.9,en-GB;q=0.8,en;q=0.7",
            "Connection": "keep-alive",
            "Content-Type": "application/json; charset=UTF-8",
            "Origin": NIFTY_BASE_URL,
            "Referer": NIFTY_HISTORY_PAGE_URL,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    # This warmup obtains the transient ASP.NET/Akamai cookies when the site
    # responds. If it is slow, continue; the API call may still work.
    for attempt in range(retries + 1):
        try:
            session.get(NIFTY_HISTORY_PAGE_URL, timeout=request_timeout(timeout))
            break
        except requests.RequestException:
            if attempt < retries:
                time.sleep(0.75 + attempt * 0.75)
    return session


def find_category(master: Dict[str, List[str]], wanted: str) -> str:
    wanted_lower = wanted.lower()
    for key in master:
        if key.lower() == wanted_lower:
            return key
    for key in master:
        if wanted_lower in key.lower():
            return key
    raise KeyError(f"Could not find category matching {wanted!r}")


def request_timeout(timeout: int) -> tuple[int, int]:
    return (min(10, timeout), timeout)


def extract_index_rows(data) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    def walk(value, category: str = "IndexMapping") -> None:
        if isinstance(value, dict):
            next_category = (
                value.get("category")
                or value.get("Category")
                or value.get("subCategory")
                or value.get("SubCategory")
                or value.get("group")
                or value.get("Group")
                or category
            )
            name = (
                value.get("indexName")
                or value.get("IndexName")
                or value.get("name")
                or value.get("Name")
                or value.get("label")
                or value.get("Label")
                or value.get("text")
                or value.get("Text")
            )
            if isinstance(name, str) and name.upper().startswith("NIFTY"):
                rows.append(
                    {
                        "index_name": " ".join(name.upper().split()),
                        "category": str(next_category),
                    }
                )
            for child in value.values():
                walk(child, str(next_category))
        elif isinstance(value, list):
            for child in value:
                walk(child, category)
        elif isinstance(value, str) and value.upper().startswith("NIFTY"):
            rows.append(
                {
                    "index_name": " ".join(value.upper().split()),
                    "category": str(category),
                }
            )

    walk(data)
    return dedupe_rows(rows)


def fetch_index_master(
    session: requests.Session, timeout: int, retries: int
) -> Dict[str, List[str]]:
    fallback = default_index_master()

    try:
        data = get_json_with_retries(
            session=session,
            url=NIFTY_INDEX_MAPPING_URL,
            timeout=timeout,
            retries=retries,
            params={"{}": "", "_": int(time.time() * 1000)},
        )
    except RuntimeError:
        return fallback

    mapping_rows = extract_index_rows(data)
    if not mapping_rows:
        return fallback

    rows_by_category: Dict[str, List[str]] = {}
    for row in mapping_rows:
        rows_by_category.setdefault(row["category"], []).append(row["index_name"])

    sector_names = set(DEFAULT_SECTOR_INDICES)
    for row in mapping_rows:
        if row["index_name"] in sector_names:
            rows_by_category.setdefault("Sectoral Indices", []).append(row["index_name"])

    rows_by_category.setdefault("Sectoral Indices", DEFAULT_SECTOR_INDICES)
    return {
        category: sorted(set(names))
        for category, names in rows_by_category.items()
        if names
    }


def default_index_master() -> Dict[str, List[str]]:
    return {"Sectoral Indices": list(DEFAULT_SECTOR_INDICES)}


def rows_from_master(master: Dict[str, List[str]]) -> List[Dict[str, str]]:
    rows = []
    for category, names in master.items():
        for name in names:
            rows.append({"index_name": name, "category": category})
    return rows


def pick_indices(master: Dict[str, List[str]], universe: str) -> List[Dict[str, str]]:
    if universe == "all":
        rows = [
            row
            for row in rows_from_master(master)
            if row["index_name"].upper().startswith("NIFTY")
        ]
    elif universe == "thematic":
        category = find_category(master, "Thematic Market Indices")
        rows = [{"index_name": name, "category": category} for name in master[category]]
    else:
        category = find_category(master, "Sectoral Indices")
        rows = [{"index_name": name, "category": category} for name in master[category]]

    return sorted(dedupe_rows(rows), key=lambda row: row["index_name"])


def dedupe_rows(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    result = []
    for row in rows:
        name = row["index_name"]
        if name in seen:
            continue
        seen.add(name)
        result.append(row)
    return result


def parse_ymd(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def to_nifty_date(value: date) -> str:
    return value.strftime("%d-%b-%Y")


def normalize_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""

    for fmt in ("%d %b %Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return value


def clean_number(value):
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return value
    text = str(value).replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return value


def get_json_with_retries(
    session: requests.Session,
    url: str,
    timeout: int,
    retries: int,
    params: Optional[dict] = None,
) -> dict:
    for attempt in range(retries + 1):
        try:
            response = session.get(url, params=params, timeout=request_timeout(timeout))
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
            time.sleep(0.5 + attempt * 0.5)
    return {}


def post_history_with_retries(
    session: requests.Session,
    index_name: str,
    start_date: date,
    end_date: date,
    timeout: int,
    retries: int,
) -> List[dict]:
    cinfo = (
        "{"
        f"'name':'{index_name}',"
        f"'startDate':'{to_nifty_date(start_date)}',"
        f"'endDate':'{to_nifty_date(end_date)}',"
        f"'indexName':'{index_name}'"
        "}"
    )
    payload = {"cinfo": cinfo}

    for attempt in range(retries + 1):
        try:
            response = session.post(
                NIFTY_HISTORY_URL,
                json=payload,
                timeout=request_timeout(timeout),
            )
            response.raise_for_status()
            data = response.json()
            value = data.get("d", data) if isinstance(data, dict) else data
            if isinstance(value, str):
                value = json.loads(value)
            return value if isinstance(value, list) else []
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            if attempt >= retries:
                raise RuntimeError(
                    f"Failed to download {index_name} history: {exc}"
                ) from exc
            time.sleep(0.75 + attempt * 0.75)
    return []


def download_index_history(
    session: requests.Session,
    index_name: str,
    start_date: date,
    end_date: date,
    timeout: int,
    retries: int,
) -> List[Dict[str, object]]:
    by_date: Dict[str, Dict[str, object]] = {}

    rows = post_history_with_retries(
        session=session,
        index_name=index_name,
        start_date=start_date,
        end_date=end_date,
        timeout=timeout,
        retries=retries,
    )

    for row in rows:
        row_date = normalize_date(
            row.get("HistoricalDate")
            or row.get("EOD_TIMESTAMP")
            or row.get("TIMESTAMP")
            or row.get("CH_TIMESTAMP")
            or row.get("Date")
        )
        if not row_date:
            continue
        by_date[row_date] = {
            "index_name": index_name,
            "date": row_date,
            "open": clean_number(
                row.get("OPEN")
                or row.get("EOD_OPEN_INDEX_VAL")
                or row.get("Open")
            ),
            "high": clean_number(
                row.get("HIGH")
                or row.get("EOD_HIGH_INDEX_VAL")
                or row.get("High")
            ),
            "low": clean_number(
                row.get("LOW")
                or row.get("EOD_LOW_INDEX_VAL")
                or row.get("Low")
            ),
            "close": clean_number(
                row.get("CLOSE")
                or row.get("EOD_CLOSE_INDEX_VAL")
                or row.get("Close")
            )
            or clean_number(row.get("Index Value")),
            "pe": "",
            "pb": "",
            "div_yield": "",
            "total_returns_index": "",
        }

    return [by_date[key] for key in sorted(by_date)]


def safe_filename(index_name: str) -> str:
    value = index_name.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") + ".csv"


def write_csv(path: Path, rows: Sequence[Dict[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_lines(path: Path, values: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(f"{value}\n")


def summarize_index(rows: Sequence[Dict[str, object]]) -> Optional[Dict[str, float]]:
    clean_rows = [
        row
        for row in rows
        if isinstance(row.get("close"), (int, float))
        and isinstance(row.get("high"), (int, float))
    ]
    if len(clean_rows) < 2:
        return None

    first_close = float(clean_rows[0]["close"])
    latest_close = float(clean_rows[-1]["close"])
    one_year_high = max(float(row["high"]) for row in clean_rows)
    if first_close <= 0 or one_year_high <= 0:
        return None

    return {
        "first_close": first_close,
        "latest_close": latest_close,
        "one_year_high": one_year_high,
        "return_pct": ((latest_close / first_close) - 1.0) * 100.0,
        "close_to_high": latest_close / one_year_high,
    }


def screen_index(
    index_name: str,
    rows: Sequence[Dict[str, object]],
    benchmark_return_pct: float,
) -> Dict[str, object]:
    stats = summarize_index(rows)
    if stats is None:
        return {
            "index_name": index_name,
            "passed": False,
            "reason": "not enough clean rows",
        }

    beats_nifty = stats["return_pct"] > benchmark_return_pct
    near_high = stats["close_to_high"] > 0.90
    reasons = []
    if beats_nifty:
        reasons.append("return>NIFTY50")
    if near_high:
        reasons.append("close>90% of 1Y high")

    return {
        "index_name": index_name,
        "passed": beats_nifty or near_high,
        "reason": ", ".join(reasons) if reasons else "failed both filters",
        **stats,
    }


def high_drawdown_pct(stats: Dict[str, object]) -> float:
    return (1.0 - float(stats["close_to_high"])) * 100.0


def format_pct(value: float) -> str:
    return f"{value:+.2f}%"


def format_report_lines(
    benchmark_stats: Dict[str, float],
    passed_results: Sequence[Dict[str, object]],
) -> List[str]:
    sorted_results = sorted(
        passed_results,
        key=lambda result: float(result["return_pct"]) - benchmark_stats["return_pct"],
        reverse=True,
    )
    lines = [
        f"{BENCHMARK_INDEX} 1Y return: {format_pct(benchmark_stats['return_pct'])}",
        (
            f"{BENCHMARK_INDEX} below 1Y high: "
            f"{high_drawdown_pct(benchmark_stats):.2f}%"
        ),
        "",
        "Passed sector indices:",
    ]

    for result in sorted_results:
        alpha = float(result["return_pct"]) - benchmark_stats["return_pct"]
        drawdown = high_drawdown_pct(result)
        lines.append(
            f"{result['index_name']} | alpha_vs_NIFTY50: {format_pct(alpha)} "
            f"| below_1Y_high: {drawdown:.2f}%"
        )

    return lines


def resolve_dates(args: argparse.Namespace) -> tuple[date, date]:
    end_date = parse_ymd(args.to_date) if args.to_date else date.today()
    start_date = (
        parse_ymd(args.from_date)
        if args.from_date
        else end_date - timedelta(days=365 * args.years)
    )
    if start_date > end_date:
        raise ValueError("--from-date cannot be after --to-date")
    if (end_date - start_date).days + 1 > MAX_HISTORY_DAYS:
        start_date = end_date - timedelta(days=MAX_HISTORY_DAYS - 1)
        print(
            f"NiftyIndices allows about 1 year per request; clamped start date to {start_date}."
        )
    return start_date, end_date


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Screen Nifty sector indices against NIFTY 50 and 1-year highs."
    )
    parser.add_argument(
        "--universe",
        choices=["sectors", "sectoral", "thematic", "all"],
        default="sectors",
        help=(
            "sectors/sectoral = sector names from NiftyIndices historical-data page; "
            "all includes any extra NIFTY names discovered from IndexMapping.json."
        ),
    )
    parser.add_argument(
        "--indices",
        nargs="+",
        help='Download only these index names, e.g. --indices "NIFTY IT" "NIFTY BANK".',
    )
    parser.add_argument(
        "--years",
        type=int,
        default=1,
        help="Years back from --to-date. NiftyIndices is clamped to about 1 year.",
    )
    parser.add_argument("--from-date", help="Start date in YYYY-MM-DD format.")
    parser.add_argument("--to-date", help="End date in YYYY-MM-DD format.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output folder.")
    parser.add_argument(
        "--passed-file",
        default=str(DEFAULT_PASSED_FILE),
        help="Text file where the passed-index report is saved.",
    )
    parser.add_argument(
        "--save-history",
        action="store_true",
        help="Also save downloaded price history CSVs. Off by default.",
    )
    parser.add_argument("--list-only", action="store_true", help="Only print index names.")
    parser.add_argument(
        "--refresh-index-map",
        action="store_true",
        help="Try live IndexMapping.json instead of the built-in sector list.",
    )
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=2, help="Retries per request.")
    parser.add_argument(
        "--pause",
        type=float,
        default=0.35,
        help="Pause between indices to stay gentle with public endpoints.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    out_dir = Path(args.out_dir)

    if args.refresh_index_map:
        mapping_session = make_nifty_indices_session(
            timeout=args.timeout,
            retries=args.retries,
        )
        master = fetch_index_master(
            session=mapping_session,
            timeout=args.timeout,
            retries=args.retries,
        )
    else:
        master = default_index_master()
    master_rows = rows_from_master(master)

    if args.indices:
        category_by_name = {row["index_name"]: row["category"] for row in master_rows}
        selected = [
            {
                "index_name": name,
                "category": category_by_name.get(name, "Manual selection"),
            }
            for name in args.indices
        ]
    else:
        selected = pick_indices(master, args.universe)

    if args.list_only:
        print(f"Found {len(selected)} indices in universe={args.universe}")
        for row in selected:
            print(f"{row['index_name']} | {row['category']}")
        return 0

    start_date, end_date = resolve_dates(args)
    nifty_session = make_nifty_indices_session(timeout=args.timeout, retries=args.retries)

    print(
        f"Screening {len(selected)} indices from {start_date} to {end_date} "
        f"against {BENCHMARK_INDEX}"
    )
    print(f"Passed-index report will be saved to: {args.passed_file}")
    write_lines(Path(args.passed_file), [])

    benchmark_rows = download_index_history(
        nifty_session,
        index_name=BENCHMARK_INDEX,
        start_date=start_date,
        end_date=end_date,
        timeout=args.timeout,
        retries=args.retries,
    )
    benchmark_stats = summarize_index(benchmark_rows)
    if benchmark_stats is None:
        raise RuntimeError(f"Could not calculate {BENCHMARK_INDEX} return.")

    benchmark_return_pct = benchmark_stats["return_pct"]
    print(
        f"{BENCHMARK_INDEX} 1Y return: {format_pct(benchmark_return_pct)} "
        f"| below 1Y high: {high_drawdown_pct(benchmark_stats):.2f}% "
        f"| latest close: {benchmark_stats['latest_close']:.2f}"
    )

    passed_results: List[Dict[str, object]] = []
    for number, row in enumerate(selected, start=1):
        index_name = row["index_name"]
        print(f"[{number}/{len(selected)}] Checking {index_name} ...")
        rows = download_index_history(
            nifty_session,
            index_name=index_name,
            start_date=start_date,
            end_date=end_date,
            timeout=args.timeout,
            retries=args.retries,
        )

        result = screen_index(index_name, rows, benchmark_return_pct)
        if not result["passed"]:
            print(f"  SKIP {index_name}: {result['reason']}")
            time.sleep(args.pause)
            continue

        passed_results.append(result)
        alpha = float(result["return_pct"]) - benchmark_return_pct
        drawdown = high_drawdown_pct(result)
        print(
            f"  PASS {index_name}: alpha_vs_NIFTY50={format_pct(alpha)} "
            f"| below_1Y_high={drawdown:.2f}% "
            f"({result['reason']})"
        )

        if args.save_history:
            index_path = out_dir / "by_index" / safe_filename(index_name)
            write_csv(index_path, rows, HISTORY_COLUMNS)
            print(f"  Saved history CSV to {index_path}")
        write_lines(
            Path(args.passed_file),
            format_report_lines(benchmark_stats, passed_results),
        )
        time.sleep(args.pause)

    report_lines = format_report_lines(benchmark_stats, passed_results)
    write_lines(Path(args.passed_file), report_lines)
    print(f"\nPassed indices: {len(passed_results)}")
    sorted_passed_results = sorted(
        passed_results,
        key=lambda result: float(result["return_pct"]) - benchmark_return_pct,
        reverse=True,
    )
    for result in sorted_passed_results:
        alpha = float(result["return_pct"]) - benchmark_return_pct
        drawdown = high_drawdown_pct(result)
        print(
            f"- {result['index_name']} | alpha_vs_NIFTY50: {format_pct(alpha)} "
            f"| below_1Y_high: {drawdown:.2f}%"
        )
    print(f"Saved passed-index report to: {args.passed_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
