from __future__ import annotations

import argparse
import csv
import json
import re
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import requests
from bs4 import BeautifulSoup

from coffeeCanInvesting_filter_stocks import (
    DATA_DIR,
    SCREENER_HEADERS,
    InputStock,
    calculate_roce,
    calculate_roe,
    clean_text,
    find_row,
    format_float,
    latest_complete_pairs,
    load_or_fetch_screener_html,
    parse_number,
    parse_screener_tables,
    read_input_stock_files,
    row_values,
    screener_html_path,
    should_refresh,
    table_years,
)


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
DEFAULT_PASSED_TXT = ROOT_DIR / "config" / "pat_growth_eps_low_passed_stocks.txt"
DEFAULT_OUTPUT_CSV = ROOT_DIR / "data" / "pat_growth_eps_low_matches.csv"
DEFAULT_OUTPUT_MD = ROOT_DIR / "data" / "pat_growth_eps_low_matches.md"

IT_KEYWORDS = (
    "information technology",
    "it - software",
    "it services",
    "computer software",
    "software",
    "infotech",
    "technology services",
    "digital services",
    "consulting and business solutions",
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str
    years: list[str] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    details: dict[str, float | str] = field(default_factory=dict)


@dataclass
class StockResult:
    symbol: str
    company_name: str
    industry: str
    passed: bool
    checks: list[CheckResult]


def year_label(year: str) -> str:
    matches = re.findall(r"\d{4}", clean_text(year))
    return matches[-1] if matches else clean_text(year)


def format_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}%"


def latest_pair(years: list[str], values: list[float | None]):
    pairs = latest_complete_pairs(years, values, 1)
    return pairs[-1] if pairs else (None, None)


def check_latest_threshold(
    name: str,
    years: list[str],
    values: list[float | None],
    threshold: float,
    unit: str,
) -> CheckResult:
    year, value = latest_pair(years, values)
    if value is None:
        return CheckResult(name, False, f"{name} latest value missing.")
    if value <= threshold:
        return CheckResult(
            name,
            False,
            f"{name} {format_float(value)}{unit} <= {threshold:g}{unit}.",
            [year],
            [value],
        )
    return CheckResult(
        name,
        True,
        f"{name} {format_float(value)}{unit} > {threshold:g}{unit}.",
        [year],
        [value],
    )


def check_strict_increase(
    name: str,
    years: list[str],
    values: list[float | None],
    count: int,
    require_positive: bool = False,
) -> CheckResult:
    pairs = latest_complete_pairs(years, values, count)
    if len(pairs) < count:
        return CheckResult(
            name,
            False,
            f"Need {count} annual {name} values; found {len(pairs)}.",
            [year for year, _ in pairs],
            [value for _, value in pairs],
        )

    selected_years = [year for year, _ in pairs]
    selected_values = [value for _, value in pairs]

    if require_positive:
        non_positive = [
            f"{year_label(year)}={format_float(value)}"
            for year, value in zip(selected_years, selected_values)
            if value <= 0
        ]
        if non_positive:
            return CheckResult(
                name,
                False,
                f"{name} must be positive in all selected years: " + "; ".join(non_positive),
                selected_years,
                selected_values,
            )

    failed = []
    for previous_year, previous, current_year, current in zip(
        selected_years,
        selected_values,
        selected_years[1:],
        selected_values[1:],
    ):
        if current <= previous:
            failed.append(f"{year_label(previous_year)}->{year_label(current_year)} {format_float(previous)} to {format_float(current)}")

    if failed:
        return CheckResult(
            name,
            False,
            f"{name} not increasing every year: " + "; ".join(failed),
            selected_years,
            selected_values,
        )

    return CheckResult(
        name,
        True,
        f"{name} increased every year for latest {count} annual values.",
        selected_years,
        selected_values,
        {"latest": selected_values[-1]},
    )


def check_latest_all_time_high(
    name: str,
    years: list[str],
    values: list[float | None],
) -> CheckResult:
    pairs = [(year, value) for year, value in zip(years, values) if value is not None]
    if not pairs:
        return CheckResult(name, False, f"{name} values missing.")

    latest_year, latest_value = pairs[-1]
    max_year, max_value = max(pairs, key=lambda item: item[1])
    if latest_value < max_value:
        return CheckResult(
            name,
            False,
            f"{name} latest {format_float(latest_value)} is below all-time high {format_float(max_value)} in {year_label(max_year)}.",
            [year for year, _ in pairs],
            [value for _, value in pairs],
            {"latest": latest_value, "all_time_high": max_value},
        )

    return CheckResult(
        name,
        True,
        f"{name} is at all-time high.",
        [latest_year],
        [latest_value],
        {"latest": latest_value, "all_time_high": max_value},
    )


def parse_about_and_classification(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    parts = []

    about = soup.select_one(".about")
    if about:
        parts.append(about.get_text(" ", strip=True))

    for title in ("Broad Sector", "Sector", "Broad Industry", "Industry"):
        node = soup.select_one(f'a[title="{title}"]')
        if node:
            parts.append(node.get_text(" ", strip=True))

    return clean_text(" ".join(parts))


def is_it_company(stock: InputStock, company_name: str, html: str) -> bool:
    text = " ".join(
        item.lower()
        for item in (
            stock.symbol,
            stock.company_name,
            company_name,
            stock.industry,
            parse_about_and_classification(html),
        )
        if item
    )
    return any(keyword in text for keyword in IT_KEYWORDS)


def check_not_it_company(stock: InputStock, company_name: str, html: str) -> CheckResult:
    if is_it_company(stock, company_name, html):
        return CheckResult("IT exclusion", False, "IT/software company excluded.")
    return CheckResult("IT exclusion", True, "Not detected as IT/software company.")


def top_ratio_value(top_ratios: dict[str, str], label: str) -> float | None:
    for key, value in top_ratios.items():
        if clean_text(key).lower() == clean_text(label).lower():
            return parse_number(value)
    return None


def latest_roe(tables: dict[str, list[list[str]]], top_ratios: dict[str, str]) -> tuple[str | None, float | None]:
    current = top_ratio_value(top_ratios, "ROE")
    if current is not None:
        return "current", current

    ratios = tables.get("ratios", [])
    row = find_row(ratios, ("ROE %", "ROE"))
    if row:
        return latest_pair(table_years(ratios), row_values(row))

    return latest_pair(*calculate_roe(tables))


def latest_roce(tables: dict[str, list[list[str]]], top_ratios: dict[str, str]) -> tuple[str | None, float | None]:
    current = top_ratio_value(top_ratios, "ROCE")
    if current is not None:
        return "current", current

    ratios = tables.get("ratios", [])
    row = find_row(ratios, ("ROCE %", "ROCE"))
    if row:
        return latest_pair(table_years(ratios), row_values(row))

    return latest_pair(*calculate_roce(tables))


def check_return_metric(name: str, year: str | None, value: float | None, threshold: float) -> CheckResult:
    if value is None:
        return CheckResult(name, False, f"{name} missing.")
    if value <= threshold:
        return CheckResult(
            name,
            False,
            f"{name} {format_pct(value)} <= {threshold:g}%.",
            [year or ""],
            [value],
        )
    return CheckResult(
        name,
        True,
        f"{name} {format_pct(value)} > {threshold:g}%.",
        [year or ""],
        [value],
    )


def check_other_income_quality(
    years: list[str],
    other_income: list[float | None],
    net_profit: list[float | None],
    threshold_pct: float,
) -> CheckResult:
    pairs = [
        (year, other, profit)
        for year, other, profit in zip(years, other_income, net_profit)
        if other is not None and profit is not None
    ]
    if not pairs:
        return CheckResult("Other income quality", False, "Other income / Net Profit data missing.")

    year, other, profit = pairs[-1]
    if profit <= 0:
        return CheckResult(
            "Other income quality",
            False,
            f"Latest Net Profit is not positive: {format_float(profit)}.",
            [year],
            [profit],
        )

    share = (other / profit) * 100
    if share > threshold_pct:
        return CheckResult(
            "Other income quality",
            False,
            f"Other Income is {format_pct(share)} of Net Profit; need <= {threshold_pct:g}%.",
            [year],
            [share],
            {"other_income": other, "net_profit": profit, "share_pct": share},
        )

    return CheckResult(
        "Other income quality",
        True,
        f"Other Income is {format_pct(share)} of Net Profit.",
        [year],
        [share],
        {"other_income": other, "net_profit": profit, "share_pct": share},
    )


def company_info(html: str) -> tuple[str | None, bool]:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#company-info")
    if not node:
        return None, True
    return node.get("data-company-id"), node.get("data-consolidated") == "true"


def chart_cache_path(data_dir: Path, symbol: str) -> Path:
    return Path(data_dir) / symbol / "screener_finance" / "chart_pe_5y.json"


def fetch_pe_chart(
    session: requests.Session,
    company_id: str,
    consolidated: bool,
    timeout: int,
) -> dict:
    params = {
        "q": "Price to Earning-Median PE-EPS",
        "days": "1825",
    }
    if consolidated:
        params["consolidated"] = "true"

    headers = dict(SCREENER_HEADERS)
    headers["X-Requested-With"] = "XMLHttpRequest"
    response = session.get(
        f"https://www.screener.in/api/company/{company_id}/chart/",
        headers=headers,
        params=params,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def load_or_fetch_pe_chart(
    session: requests.Session,
    symbol: str,
    html: str,
    data_dir: Path,
    fetch_missing: bool,
    refresh_days: int,
    timeout: int,
) -> dict:
    path = chart_cache_path(data_dir, symbol)
    if fetch_missing and should_refresh(path, refresh_days):
        company_id, consolidated = company_info(html)
        if not company_id:
            raise ValueError("Screener company id missing; cannot fetch PE chart.")
        data = fetch_pe_chart(session, company_id, consolidated, timeout)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data

    if not path.exists():
        raise FileNotFoundError(f"PE chart cache not found: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


def metric_values(chart_data: dict, metric: str) -> list[tuple[str, float]]:
    for dataset in chart_data.get("datasets", []):
        if dataset.get("metric") != metric:
            continue
        values = []
        for point in dataset.get("values", []):
            if len(point) < 2:
                continue
            value = parse_number(point[1])
            if value is not None:
                values.append((clean_text(point[0]), value))
        return values
    return []


def check_eps_from_chart(chart_data: dict, tolerance_pct: float) -> CheckResult:
    values = metric_values(chart_data, "EPS")
    if not values:
        return CheckResult("TTM EPS high", False, "EPS chart data missing.")

    latest_date, latest_value = values[-1]
    max_date, max_value = max(values, key=lambda item: item[1])
    allowed_gap = abs(max_value) * (tolerance_pct / 100)
    if latest_value + allowed_gap < max_value:
        return CheckResult(
            "TTM EPS high",
            False,
            f"TTM EPS latest {format_float(latest_value)} below 5-year high {format_float(max_value)} on {max_date}.",
            [date for date, _ in values],
            [value for _, value in values],
            {"latest": latest_value, "five_year_high": max_value},
        )

    return CheckResult(
        "TTM EPS high",
        True,
        "TTM EPS is at 5-year high.",
        [latest_date],
        [latest_value],
        {"latest": latest_value, "five_year_high": max_value},
    )


def check_pe_low(chart_data: dict, tolerance_pct: float) -> CheckResult:
    values = metric_values(chart_data, "Price to Earning")
    if len(values) < 2:
        return CheckResult("PE low", False, "Need PE chart data for 5-year low check.")

    first_date, first_value = values[0]
    latest_date, latest_value = values[-1]
    low_date, low_value = min(values, key=lambda item: item[1])
    allowed_gap = abs(low_value) * (tolerance_pct / 100)

    if latest_value > low_value + allowed_gap:
        return CheckResult(
            "PE low",
            False,
            f"PE latest {format_float(latest_value)} is above 5-year low {format_float(low_value)} on {low_date}.",
            [date for date, _ in values],
            [value for _, value in values],
            {"latest": latest_value, "five_year_low": low_value},
        )

    if latest_value >= first_value:
        return CheckResult(
            "PE low",
            False,
            f"PE is at low but not below start of 5-year range: {format_float(latest_value)} vs {format_float(first_value)} on {first_date}.",
            [first_date, latest_date],
            [first_value, latest_value],
            {"latest": latest_value, "first": first_value, "five_year_low": low_value},
        )

    return CheckResult(
        "PE low",
        True,
        "PE is at 5-year low and below the start of the 5-year range.",
        [latest_date],
        [latest_value],
        {"latest": latest_value, "five_year_low": low_value, "first": first_value},
    )


def evaluate_stock(stock: InputStock, html: str, chart_data: dict, args) -> StockResult:
    screener_company_name, tables, top_ratios = parse_screener_tables(html)
    company_name = stock.company_name or screener_company_name

    profit_loss = tables.get("profit-loss", [])
    years = table_years(profit_loss)
    sales = row_values(find_row(profit_loss, ("Sales +", "Sales", "Revenue +", "Revenue")))
    pat = row_values(find_row(profit_loss, ("Net Profit +", "Net Profit")))
    eps = row_values(find_row(profit_loss, ("EPS in Rs", "EPS")))
    other_income = row_values(find_row(profit_loss, ("Other Income +", "Other Income")))

    roe_year, roe_value = latest_roe(tables, top_ratios)
    roce_year, roce_value = latest_roce(tables, top_ratios)

    checks = [
        check_not_it_company(stock, company_name, html),
        check_latest_threshold("Sales", years, sales, args.min_sales_cr, " Cr"),
        check_strict_increase("PAT", years, pat, args.years, require_positive=True),
        check_latest_all_time_high("Sales", years, sales),
        check_latest_all_time_high("PAT", years, pat),
        check_strict_increase("Annual EPS", years, eps, args.years, require_positive=True),
        check_eps_from_chart(chart_data, args.eps_high_tolerance_pct),
        check_pe_low(chart_data, args.pe_low_tolerance_pct),
        check_return_metric("ROE", roe_year, roe_value, args.min_roe),
        check_return_metric("ROCE", roce_year, roce_value, args.min_roce),
        check_other_income_quality(years, other_income, pat, args.max_other_income_pct),
    ]

    return StockResult(
        symbol=stock.symbol,
        company_name=company_name,
        industry=stock.industry,
        passed=all(check.passed for check in checks),
        checks=checks,
    )


def failed_checks(result: StockResult) -> list[CheckResult]:
    return [check for check in result.checks if not check.passed]


def result_detail(result: StockResult) -> str:
    failures = failed_checks(result)
    if not failures:
        return ""
    return " | ".join(f"{check.name}: {check.reason}" for check in failures)


def write_csv(results: list[StockResult], output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    check_names = []
    for result in results:
        for check in result.checks:
            if check.name not in check_names:
                check_names.append(check.name)

    fieldnames = ["symbol", "company_name", "industry", "passed", "failed_reasons"]
    for name in check_names:
        key = name.lower().replace(" ", "_")
        fieldnames.extend([f"{key}_passed", f"{key}_reason", f"{key}_values"])

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            row = {
                "symbol": result.symbol,
                "company_name": result.company_name,
                "industry": result.industry,
                "passed": "YES" if result.passed else "NO",
                "failed_reasons": result_detail(result),
            }
            by_name = {check.name: check for check in result.checks}
            for name in check_names:
                check = by_name.get(name)
                key = name.lower().replace(" ", "_")
                if not check:
                    row[f"{key}_passed"] = ""
                    row[f"{key}_reason"] = ""
                    row[f"{key}_values"] = ""
                    continue
                row[f"{key}_passed"] = "YES" if check.passed else "NO"
                row[f"{key}_reason"] = check.reason
                row[f"{key}_values"] = "; ".join(
                    f"{year_label(year)}={format_float(value)}"
                    for year, value in zip(check.years, check.values)
                )
            writer.writerow(row)


def write_markdown(results: list[StockResult], output_path: Path):
    passed = [result for result in results if result.passed]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# PAT Growth + EPS High + PE Low Matches",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"Matched stocks: {len(passed)} / {len(results)}",
        "",
        "| Symbol | Company | Industry |",
        "|---|---|---|",
    ]
    if passed:
        for result in passed:
            lines.append(f"| {result.symbol} | {result.company_name} | {result.industry} |")
    else:
        lines.append("| - | - | - |")

    lines.extend(
        [
            "",
            "## Failed / Skipped Reasons",
            "",
            "| Symbol | Reasons |",
            "|---|---|",
        ]
    )
    for result in results:
        if result.passed:
            continue
        lines.append(f"| {result.symbol} | {result_detail(result)} |")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_passed_txt(results: list[StockResult], output_path: Path):
    passed = [result for result in results if result.passed]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Passed stocks for PAT growth + EPS high + PE low strategy",
        f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Count: {len(passed)}",
        "",
    ]
    lines.extend(result.symbol for result in passed)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def process_stocks(args) -> list[StockResult]:
    if args.symbols:
        stocks = [InputStock(symbol=clean_text(symbol).upper()) for symbol in args.symbols]
    else:
        stocks = read_input_stock_files(args.csv_file)
    if args.limit:
        stocks = stocks[: args.limit]

    session = requests.Session()
    results = []
    passed_symbols = []

    for index, stock in enumerate(stocks, start=1):
        html_path = screener_html_path(args.data_dir, stock.symbol)
        source = (
            "local only"
            if args.no_fetch
            else "fetching Screener"
            if should_refresh(html_path, args.refresh_days)
            else "using local Screener file"
        )
        print(f"[{index}/{len(stocks)}] {stock.symbol}: working... ({source})", flush=True)

        try:
            html = load_or_fetch_screener_html(
                session=session,
                symbol=stock.symbol,
                data_dir=args.data_dir,
                fetch_missing=not args.no_fetch,
                refresh_days=args.refresh_days,
                timeout=args.timeout,
            )
            chart_data = load_or_fetch_pe_chart(
                session=session,
                symbol=stock.symbol,
                html=html,
                data_dir=args.data_dir,
                fetch_missing=not args.no_fetch,
                refresh_days=args.refresh_days,
                timeout=args.timeout,
            )
            result = evaluate_stock(stock, html, chart_data, args)
            status = "PASS" if result.passed else "FAIL"
            print(f"[{index}/{len(stocks)}] {stock.symbol}: {status}", flush=True)
            if not result.passed:
                for check in failed_checks(result):
                    print(f"  - {check.name}: {check.reason}", flush=True)
            else:
                passed_symbols.append(result.symbol)
                print("Passed so far: " + ", ".join(passed_symbols), flush=True)
            results.append(result)
        except Exception as exc:
            reason = str(exc)
            print(f"[{index}/{len(stocks)}] {stock.symbol}: ERROR - {reason}", flush=True)
            results.append(
                StockResult(
                    symbol=stock.symbol,
                    company_name=stock.company_name,
                    industry=stock.industry,
                    passed=False,
                    checks=[CheckResult("Processing", False, reason)],
                )
            )

        if args.sleep > 0 and index < len(stocks):
            time.sleep(args.sleep)

    return results


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Filter NSE stocks where PAT and annual EPS rise every year for the "
            "latest 5 annual values, sales/PAT are all-time highs, current sales "
            ">250 Cr, TTM EPS is at 5-year high, PE is at 5-year low, ROE/ROCE "
            ">10%, other income is <=20% of PAT, and IT companies are excluded."
        )
    )
    parser.add_argument(
        "--csv-file",
        nargs="+",
        default=[],
        help="One or more input CSV files with Symbol or Ticker column.",
    )
    parser.add_argument("--symbols", nargs="+", default=[], help="Manual symbols to scan without an input CSV.")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--passed-txt", type=Path, default=DEFAULT_PASSED_TXT)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_OUTPUT_MD)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--min-sales-cr", type=float, default=250.0)
    parser.add_argument("--min-roe", type=float, default=10.0)
    parser.add_argument("--min-roce", type=float, default=10.0)
    parser.add_argument("--max-other-income-pct", type=float, default=20.0)
    parser.add_argument(
        "--pe-low-tolerance-pct",
        type=float,
        default=0.5,
        help="Allowed gap from exact 5-year PE low. Default: 0.5%%.",
    )
    parser.add_argument(
        "--eps-high-tolerance-pct",
        type=float,
        default=0.5,
        help="Allowed gap from exact 5-year TTM EPS high. Default: 0.5%%.",
    )
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=30,
        help="Refresh Screener HTML and PE chart cache if older than this many days. Use -1 to never refresh existing files.",
    )
    parser.add_argument("--no-fetch", action="store_true", help="Use only existing local Screener HTML/chart cache.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--sleep", type=float, default=1.0, help="Delay between Screener requests.")
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N input stocks; useful for smoke tests.")
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.csv_file and not args.symbols:
        raise SystemExit("Provide --csv-file or --symbols.")

    results = process_stocks(args)
    results.sort(key=lambda item: (not item.passed, item.symbol))

    write_csv(results, args.output_csv)
    write_markdown(results, args.output_md)
    write_passed_txt(results, args.passed_txt)

    passed = [result for result in results if result.passed]
    print("\nFinal PASS stocks:")
    if passed:
        for result in passed:
            print(f"{result.symbol} | {result.company_name or '-'} | {result.industry or '-'}")
    else:
        print("No stocks passed the filter.")

    print(f"\nMatched stocks: {len(passed)} / {len(results)}")
    print(f"Passed tickers output: {args.passed_txt}")
    print(f"CSV output: {args.output_csv}")
    print(f"Markdown output: {args.output_md}")


if __name__ == "__main__":
    main()
