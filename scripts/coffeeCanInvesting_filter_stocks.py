from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import warnings
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import requests
from bs4 import BeautifulSoup


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
DATA_DIR = ROOT_DIR / "data"
DEFAULT_PASSED_TXT = ROOT_DIR / "config" / "ten_year_strategy_passed_stocks.txt"
DEFAULT_OUTPUT_CSV = ROOT_DIR / "data" / "ten_year_strategy_matches.csv"
DEFAULT_OUTPUT_MD = ROOT_DIR / "data" / "ten_year_strategy_matches.md"
DEFAULT_MIN_MARKET_CAP_CR = 100.0

SCREENER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TABLE_IDS = (
    "profit-loss",
    "balance-sheet",
    "ratios",
)

FINANCIAL_KEYWORDS = (
    "bank",
    "finance",
    "financial",
    "finserv",
    "nbfc",
    "insurance",
    "asset management",
    "capital market",
    "broking",
    "lending",
    "credit",
)


@dataclass
class InputStock:
    symbol: str
    company_name: str = ""
    industry: str = ""


@dataclass
class SeriesCheck:
    passed: bool
    reason: str
    years: list[str] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    growth_rates: list[float] = field(default_factory=list)
    exempt_growth_years: list[str] = field(default_factory=list)
    min_value: float | None = None
    min_growth: float | None = None


@dataclass
class StockResult:
    symbol: str
    company_name: str
    industry: str
    is_financial: bool
    metric_name: str
    market_cap_cr: float | None
    market_cap_passed: bool
    market_cap_reason: str
    passed: bool
    sales_check: SeriesCheck
    metric_check: SeriesCheck


def clean_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_header(text):
    return re.sub(r"[^a-z0-9]+", "", clean_text(text).lower())


def normalize_symbol(symbol):
    return clean_text(symbol).upper()


def parse_number(value):
    text = clean_text(value)
    if not text or text in {"-", "--"}:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace(",", "").replace("%", "").replace("x", "")
    text = text.replace("Cr.", "").replace("Cr", "")
    text = text.strip()

    if not text:
        return None

    try:
        number = float(text)
    except ValueError:
        return None

    return -number if negative else number


def read_input_stocks(csv_file):
    path = Path(csv_file)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        normalized = {normalize_header(name): name for name in fieldnames}

        symbol_key = normalized.get("symbol") or normalized.get("ticker")
        if not symbol_key:
            raise ValueError("CSV must contain a Symbol or Ticker column.")

        company_key = (
            normalized.get("companyname")
            or normalized.get("company")
            or normalized.get("name")
        )
        industry_key = (
            normalized.get("industry")
            or normalized.get("sector")
            or normalized.get("business")
        )

        stocks = []
        seen = set()
        for row in reader:
            symbol = normalize_symbol(row.get(symbol_key))
            if not symbol or symbol in seen:
                continue

            seen.add(symbol)
            stocks.append(
                InputStock(
                    symbol=symbol,
                    company_name=clean_text(row.get(company_key)) if company_key else "",
                    industry=clean_text(row.get(industry_key)) if industry_key else "",
                )
            )

    return stocks


def read_input_stock_files(csv_files):
    stocks = []
    seen = set()

    for csv_file in csv_files:
        for stock in read_input_stocks(csv_file):
            if stock.symbol in seen:
                continue
            seen.add(stock.symbol)
            stocks.append(stock)

    return stocks


def screener_html_path(data_dir, symbol, consolidated=True):
    filename = "company_page.html" if consolidated else "company_page_standalone.html"
    return Path(data_dir) / symbol / "screener_finance" / filename


def should_refresh(path, max_age_days):
    if not path.exists():
        return True

    if max_age_days < 0:
        return False

    modified = datetime.fromtimestamp(path.stat().st_mtime)
    return modified < datetime.now() - timedelta(days=max_age_days)


def fetch_screener_page(session, symbol, data_dir, timeout, consolidated=True):
    suffix = "/consolidated/" if consolidated else "/"
    url = f"https://www.screener.in/company/{quote(symbol, safe='')}{suffix}"
    response = session.get(url, headers=SCREENER_HEADERS, timeout=timeout)
    response.raise_for_status()

    path = screener_html_path(data_dir, symbol, consolidated=consolidated)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(response.text, encoding="utf-8")
    return response.text


def load_or_fetch_screener_html(session, symbol, data_dir, fetch_missing, refresh_days, timeout, consolidated=True):
    path = screener_html_path(data_dir, symbol, consolidated=consolidated)
    if fetch_missing and should_refresh(path, refresh_days):
        return fetch_screener_page(session, symbol, data_dir, timeout, consolidated=consolidated)

    if not path.exists():
        raise FileNotFoundError(f"Screener HTML not found: {path}")

    return path.read_text(encoding="utf-8", errors="ignore")


def table_to_matrix(table):
    rows = []
    for tr in table.select("tr"):
        cells = [clean_text(cell.get_text(" ", strip=True)) for cell in tr.select("th,td")]
        if any(cells):
            rows.append(cells)
    return rows


def parse_screener_tables(html):
    soup = BeautifulSoup(html, "html.parser")
    tables = {}
    for table_id in TABLE_IDS:
        table = soup.select_one(f"#{table_id}")
        if table:
            tables[table_id] = table_to_matrix(table)

    company_node = soup.select_one("h1")
    company_name = clean_text(company_node.get_text(" ", strip=True)) if company_node else ""

    top_ratios = {}
    for li in soup.select("li.flex.flex-space-between"):
        name_node = li.select_one(".name")
        value_node = li.select_one(".number")
        name = clean_text(name_node.get_text(" ", strip=True)) if name_node else ""
        value = clean_text(value_node.get_text(" ", strip=True)) if value_node else ""
        if name and value:
            top_ratios[name] = value

    return company_name, tables, top_ratios


def find_row(table, labels):
    label_keys = [normalize_header(label) for label in labels]
    for row in table or []:
        if not row:
            continue

        row_key = normalize_header(row[0])
        if row_key in label_keys:
            return row

    return None


def table_years(table):
    if not table:
        return []
    return table[0][1:]


def row_values(row):
    return [parse_number(value) for value in (row or [])[1:]]


def latest_complete_pairs(years, values, count):
    pairs = [
        (year, value)
        for year, value in zip(years, values)
        if clean_text(year) and value is not None
    ]
    return pairs[-count:]


def fiscal_year_end_year(year):
    matches = re.findall(r"\d{4}", clean_text(year))
    return int(matches[-1]) if matches else None


def check_growth_series(years, values, periods, threshold):
    covid_exception_year = 2021
    needed_values = periods + 1
    pairs = latest_complete_pairs(years, values, needed_values)
    if len(pairs) < needed_values:
        return SeriesCheck(
            passed=False,
            reason=f"Need {needed_values} annual values for {periods} YoY checks; found {len(pairs)}.",
            years=[year for year, _ in pairs],
            values=[value for _, value in pairs],
        )

    selected_years = [year for year, _ in pairs]
    selected_values = [value for _, value in pairs]
    growth_rates = []

    for previous, current in zip(selected_values, selected_values[1:]):
        if previous <= 0:
            growth_rates.append(float("-inf"))
            continue
        growth_rates.append(((current - previous) / previous) * 100)

    min_growth = min(growth_rates) if growth_rates else None
    exempt_growth_years = [
        selected_years[index + 1]
        for index, growth in enumerate(growth_rates)
        if growth < threshold
        and fiscal_year_end_year(selected_years[index + 1]) == covid_exception_year
    ]
    failed_years = [
        selected_years[index + 1]
        for index, growth in enumerate(growth_rates)
        if growth < threshold
        and fiscal_year_end_year(selected_years[index + 1]) != covid_exception_year
    ]

    if failed_years:
        reason = (
            f"Sales growth below {threshold:g}% in "
            f"{', '.join(failed_years)}."
        )
        passed = False
    elif exempt_growth_years:
        reason = (
            f"All non-COVID YoY sales growth rates are >= {threshold:g}%; "
            f"FY2021 sales growth below {threshold:g}% allowed as COVID lockdown exception."
        )
        passed = True
    else:
        reason = f"All {periods} YoY sales growth rates are >= {threshold:g}%."
        passed = True

    return SeriesCheck(
        passed=passed,
        reason=reason,
        years=selected_years,
        values=selected_values,
        growth_rates=growth_rates,
        exempt_growth_years=exempt_growth_years,
        min_growth=min_growth,
    )


def check_threshold_series(years, values, count, threshold, metric_name):
    pairs = latest_complete_pairs(years, values, count)
    if len(pairs) < count:
        return SeriesCheck(
            passed=False,
            reason=f"Need {count} annual {metric_name} values; found {len(pairs)}.",
            years=[year for year, _ in pairs],
            values=[value for _, value in pairs],
        )

    selected_years = [year for year, _ in pairs]
    selected_values = [value for _, value in pairs]
    min_value = min(selected_values)
    failed_years = [
        year
        for year, value in pairs
        if value < threshold
    ]

    if failed_years:
        reason = (
            f"{metric_name} below {threshold:g}% in "
            f"{', '.join(failed_years)}."
        )
        passed = False
    else:
        reason = f"All {count} annual {metric_name} values are >= {threshold:g}%."
        passed = True

    return SeriesCheck(
        passed=passed,
        reason=reason,
        years=selected_years,
        values=selected_values,
        min_value=min_value,
    )


def check_market_cap(top_ratios, threshold):
    market_cap = parse_number(top_ratios.get("Market Cap"))
    if market_cap is None:
        return None, False, "Market Cap missing."

    if market_cap <= threshold:
        return market_cap, False, f"Market Cap <= {threshold:g} Cr."

    return market_cap, True, f"Market Cap > {threshold:g} Cr."


def add_series(left, right):
    max_len = max(len(left), len(right))
    values = []
    for index in range(max_len):
        left_value = left[index] if index < len(left) else None
        right_value = right[index] if index < len(right) else None
        if left_value is None and right_value is None:
            values.append(None)
        else:
            values.append((left_value or 0) + (right_value or 0))
    return values


def calculate_roe(tables):
    profit_loss = tables.get("profit-loss", [])
    balance_sheet = tables.get("balance-sheet", [])
    years = table_years(profit_loss)

    net_profit = row_values(find_row(profit_loss, ("Net Profit +", "Net Profit")))
    equity_capital = row_values(find_row(balance_sheet, ("Equity Capital",)))
    reserves = row_values(find_row(balance_sheet, ("Reserves",)))
    shareholder_equity = add_series(equity_capital, reserves)

    roe_values = []
    for index, profit in enumerate(net_profit):
        if profit is None or index >= len(shareholder_equity):
            roe_values.append(None)
            continue

        current_equity = shareholder_equity[index]
        previous_equity = shareholder_equity[index - 1] if index > 0 else None
        if current_equity is None or current_equity <= 0:
            roe_values.append(None)
            continue

        average_equity = (
            (previous_equity + current_equity) / 2
            if previous_equity is not None and previous_equity > 0
            else current_equity
        )
        roe_values.append((profit / average_equity) * 100)

    return years, roe_values


def calculate_roce(tables):
    profit_loss = tables.get("profit-loss", [])
    balance_sheet = tables.get("balance-sheet", [])
    years = table_years(profit_loss)

    pbt = row_values(find_row(profit_loss, ("Profit before tax",)))
    interest = row_values(find_row(profit_loss, ("Interest",)))
    equity_capital = row_values(find_row(balance_sheet, ("Equity Capital",)))
    reserves = row_values(find_row(balance_sheet, ("Reserves",)))
    borrowings = row_values(find_row(balance_sheet, ("Borrowings +", "Borrowing", "Borrowings")))
    deposits = row_values(find_row(balance_sheet, ("Deposits",)))

    shareholder_equity = add_series(equity_capital, reserves)
    debt_capital = add_series(borrowings, deposits)
    capital_employed = add_series(shareholder_equity, debt_capital)

    roce_values = []
    for index, profit_before_tax in enumerate(pbt):
        if profit_before_tax is None or index >= len(capital_employed):
            roce_values.append(None)
            continue

        capital = capital_employed[index]
        previous_capital = capital_employed[index - 1] if index > 0 else None
        if capital is None or capital <= 0:
            roce_values.append(None)
            continue

        average_capital = (
            (previous_capital + capital) / 2
            if previous_capital is not None and previous_capital > 0
            else capital
        )
        current_interest = interest[index] if index < len(interest) and interest[index] is not None else 0
        roce_values.append(((profit_before_tax + current_interest) / average_capital) * 100)

    return years, roce_values


def get_metric_series(tables, is_financial):
    ratios = tables.get("ratios", [])
    ratio_years = table_years(ratios)

    if is_financial:
        ratio_row = find_row(ratios, ("ROCE %", "ROCE"))
        if ratio_row:
            return "ROCE", ratio_years, row_values(ratio_row)
        years, values = calculate_roce(tables)
        return "ROCE", years, values

    ratio_row = find_row(ratios, ("ROE %", "ROE"))
    if ratio_row:
        return "ROE", ratio_years, row_values(ratio_row)

    years, values = calculate_roe(tables)
    return "ROE", years, values


def detect_financial(stock, tables):
    text = " ".join(
        item.lower()
        for item in (stock.symbol, stock.company_name, stock.industry)
        if item
    )
    if any(keyword in text for keyword in FINANCIAL_KEYWORDS):
        return True

    ratios = tables.get("ratios", [])
    return bool(find_row(ratios, ("ROE %", "ROE"))) and not find_row(ratios, ("ROCE %", "ROCE"))


def sales_growth_check(tables, years, growth_threshold):
    profit_loss = tables.get("profit-loss", [])
    sales_row = find_row(profit_loss, ("Sales +", "Sales", "Revenue +", "Revenue"))
    return check_growth_series(
        years=table_years(profit_loss),
        values=row_values(sales_row),
        periods=years,
        threshold=growth_threshold,
    )


def choose_sales_check(consolidated_check, standalone_check=None, standalone_error=None):
    if consolidated_check.passed:
        return replace(
            consolidated_check,
            reason=f"Consolidated: {consolidated_check.reason}",
        )

    if standalone_check is None:
        reason = f"Consolidated failed: {consolidated_check.reason}"
        if standalone_error:
            reason += f" Standalone fallback unavailable: {standalone_error}"
        return replace(consolidated_check, reason=reason)

    if standalone_check.passed:
        return replace(
            standalone_check,
            reason=(
                f"Standalone passed after consolidated failed. "
                f"Standalone: {standalone_check.reason} "
                f"Consolidated failed: {consolidated_check.reason}"
            ),
        )

    return replace(
        standalone_check,
        reason=(
            f"Consolidated failed: {consolidated_check.reason} "
            f"Standalone failed: {standalone_check.reason}"
        ),
    )


def evaluate_stock(
    stock,
    html,
    years,
    growth_threshold,
    metric_threshold,
    market_cap_threshold,
    standalone_html=None,
    standalone_error=None,
):
    screener_company_name, tables, top_ratios = parse_screener_tables(html)
    company_name = stock.company_name or screener_company_name
    market_cap_cr, market_cap_passed, market_cap_reason = check_market_cap(
        top_ratios,
        market_cap_threshold,
    )

    consolidated_sales_check = sales_growth_check(tables, years, growth_threshold)
    standalone_sales_check = None
    if standalone_html:
        _, standalone_tables, _ = parse_screener_tables(standalone_html)
        standalone_sales_check = sales_growth_check(standalone_tables, years, growth_threshold)

    sales_check = choose_sales_check(
        consolidated_check=consolidated_sales_check,
        standalone_check=standalone_sales_check,
        standalone_error=standalone_error,
    )

    is_financial = detect_financial(stock, tables)
    metric_name, metric_years, metric_values = get_metric_series(tables, is_financial)
    metric_check = check_threshold_series(
        years=metric_years,
        values=metric_values,
        count=years,
        threshold=metric_threshold,
        metric_name=metric_name,
    )

    return StockResult(
        symbol=stock.symbol,
        company_name=company_name,
        industry=stock.industry,
        is_financial=is_financial,
        metric_name=metric_name,
        market_cap_cr=market_cap_cr,
        market_cap_passed=market_cap_passed,
        market_cap_reason=market_cap_reason,
        passed=market_cap_passed and sales_check.passed and metric_check.passed,
        sales_check=sales_check,
        metric_check=metric_check,
    )


def format_float(value, decimals=2):
    if value is None:
        return ""
    return f"{value:.{decimals}f}"


def year_label(year):
    matches = re.findall(r"\d{4}", clean_text(year))
    return matches[-1] if matches else clean_text(year)


def format_pct(value):
    if value == float("-inf"):
        return "-inf%"
    if value is None:
        return ""
    return f"{value:.2f}%"


def failed_sales_points(check, threshold):
    points = []
    if len(check.years) < 2:
        return points

    exempt_years = set(check.exempt_growth_years)
    for index, growth in enumerate(check.growth_rates):
        year = check.years[index + 1]
        if growth < threshold and year not in exempt_years:
            points.append(f"{year_label(year)}->{format_pct(growth)}")

    return points


def failed_metric_points(check, threshold):
    points = []
    for year, value in zip(check.years, check.values):
        if value < threshold:
            points.append(f"{year_label(year)}->{format_pct(value)}")

    return points


def fail_detail(result, sales_threshold, metric_threshold):
    parts = []

    if not result.market_cap_passed:
        market_cap = (
            "missing"
            if result.market_cap_cr is None
            else f"{format_float(result.market_cap_cr)}Cr"
        )
        parts.append(f"market_cap->{market_cap}")

    sales_points = failed_sales_points(result.sales_check, sales_threshold)
    if sales_points:
        parts.append("sales->" + ", ".join(sales_points))
    elif not result.sales_check.passed:
        parts.append("sales->" + result.sales_check.reason)

    metric_points = failed_metric_points(result.metric_check, metric_threshold)
    if metric_points:
        parts.append(f"{result.metric_name}->" + ", ".join(metric_points))
    elif not result.metric_check.passed:
        parts.append(f"{result.metric_name}->" + result.metric_check.reason)

    return " (" + "), (".join(parts) + ")" if parts else ""


def display_points(points):
    return ", ".join(point.replace("->", ": ") for point in points)


def fail_table_rows(result, sales_threshold, metric_threshold, market_cap_threshold):
    rows = []

    if not result.market_cap_passed:
        market_cap = (
            "missing"
            if result.market_cap_cr is None
            else f"{format_float(result.market_cap_cr)} Cr"
        )
        rows.append(("Market Cap", f"{market_cap} | need > {market_cap_threshold:g} Cr"))

    sales_points = failed_sales_points(result.sales_check, sales_threshold)
    if sales_points:
        rows.append((f"Sales growth < {sales_threshold:g}%", display_points(sales_points)))
    elif not result.sales_check.passed:
        rows.append((f"Sales growth < {sales_threshold:g}%", result.sales_check.reason))

    metric_points = failed_metric_points(result.metric_check, metric_threshold)
    if metric_points:
        rows.append((f"{result.metric_name} < {metric_threshold:g}%", display_points(metric_points)))
    elif not result.metric_check.passed:
        rows.append((f"{result.metric_name} < {metric_threshold:g}%", result.metric_check.reason))

    return rows


def print_fail_table(result, sales_threshold, metric_threshold, market_cap_threshold):
    rows = fail_table_rows(result, sales_threshold, metric_threshold, market_cap_threshold)
    if not rows:
        return

    label_width = max(len("Check"), *(len(label) for label, _ in rows))
    print(f"  {'Check'.ljust(label_width)} | Failed data", flush=True)
    print(f"  {'-' * label_width} | -----------", flush=True)
    for label, detail in rows:
        print(f"  {label.ljust(label_width)} | {detail}", flush=True)


def write_csv(results, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "symbol",
                "company_name",
                "industry",
                "passed",
                "market_cap_cr",
                "market_cap_reason",
                "financial_stock",
                "metric_used",
                "sales_years",
                "sales_values",
                "min_sales_growth_pct",
                "metric_years",
                "metric_values_pct",
                "min_metric_pct",
                "sales_reason",
                "metric_reason",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "symbol": result.symbol,
                    "company_name": result.company_name,
                    "industry": result.industry,
                    "passed": "YES" if result.passed else "NO",
                    "market_cap_cr": format_float(result.market_cap_cr),
                    "market_cap_reason": result.market_cap_reason,
                    "financial_stock": "YES" if result.is_financial else "NO",
                    "metric_used": result.metric_name,
                    "sales_years": "; ".join(result.sales_check.years),
                    "sales_values": "; ".join(format_float(value, 0) for value in result.sales_check.values),
                    "min_sales_growth_pct": format_float(result.sales_check.min_growth),
                    "metric_years": "; ".join(result.metric_check.years),
                    "metric_values_pct": "; ".join(format_float(value) for value in result.metric_check.values),
                    "min_metric_pct": format_float(result.metric_check.min_value),
                    "sales_reason": result.sales_check.reason,
                    "metric_reason": result.metric_check.reason,
                }
            )


def write_markdown(results, output_path):
    passed = [result for result in results if result.passed]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# 10 Year Sales + Return Strategy Matches",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"Matched stocks: {len(passed)} / {len(results)}",
        "",
        "| Symbol | Company | Industry | Market Cap Cr | Metric | Min Sales Growth | Min Metric |",
        "|---|---|---|---:|---|---:|---:|",
    ]

    for result in passed:
        lines.append(
            "| {symbol} | {company} | {industry} | {market_cap} | {metric} | {sales}% | {metric_value}% |".format(
                symbol=result.symbol,
                company=result.company_name,
                industry=result.industry,
                market_cap=format_float(result.market_cap_cr),
                metric=result.metric_name,
                sales=format_float(result.sales_check.min_growth),
                metric_value=format_float(result.metric_check.min_value),
            )
        )

    if not passed:
        lines.append("| - | - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## Failed / Skipped Reasons",
            "",
            "| Symbol | Market Cap Check | Sales Check | Metric Check |",
            "|---|---|---|---|",
        ]
    )
    for result in results:
        if result.passed:
            continue
        lines.append(
            f"| {result.symbol} | {result.market_cap_reason} | {result.sales_check.reason} | {result.metric_check.reason} |"
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_passed_txt(results, output_path):
    passed = [result for result in results if result.passed]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Passed stocks for 10 year sales + return strategy",
        f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Count: {len(passed)}",
        "",
    ]
    lines.extend(result.symbol for result in passed)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_passed_summary(results):
    passed = [result for result in results if result.passed]

    print("\nFinal PASS stocks:")
    if not passed:
        print("No stocks passed the filter.")
        return

    for result in passed:
        print(
            "{symbol} | {company} | market cap={market_cap}Cr | {metric} min={metric_value}% | sales min growth={sales}%".format(
                symbol=result.symbol,
                company=result.company_name or "-",
                market_cap=format_float(result.market_cap_cr),
                metric=result.metric_name,
                metric_value=format_float(result.metric_check.min_value),
                sales=format_float(result.sales_check.min_growth),
            )
        )


def process_stocks(args):
    stocks = read_input_stock_files(args.csv_file)
    session = requests.Session()
    results = []
    passed_symbols = []

    for index, stock in enumerate(stocks, start=1):
        html_path = screener_html_path(args.data_dir, stock.symbol)
        if args.no_fetch:
            source = "local only"
        elif should_refresh(html_path, args.refresh_days):
            source = "fetching Screener"
        else:
            source = "using local Screener file"

        print(
            f"[{index}/{len(stocks)}] {stock.symbol}: working... ({source})",
            flush=True,
        )

        try:
            html = load_or_fetch_screener_html(
                session=session,
                symbol=stock.symbol,
                data_dir=args.data_dir,
                fetch_missing=not args.no_fetch,
                refresh_days=args.refresh_days,
                timeout=args.timeout,
            )
            result = evaluate_stock(
                stock=stock,
                html=html,
                years=args.years,
                growth_threshold=args.sales_growth,
                metric_threshold=args.return_threshold,
                market_cap_threshold=args.min_market_cap,
            )
            if not result.sales_check.passed:
                standalone_html = None
                standalone_error = None
                try:
                    standalone_html = load_or_fetch_screener_html(
                        session=session,
                        symbol=stock.symbol,
                        data_dir=args.data_dir,
                        fetch_missing=not args.no_fetch,
                        refresh_days=args.refresh_days,
                        timeout=args.timeout,
                        consolidated=False,
                    )
                except Exception as exc:
                    standalone_error = str(exc)

                result = evaluate_stock(
                    stock=stock,
                    html=html,
                    years=args.years,
                    growth_threshold=args.sales_growth,
                    metric_threshold=args.return_threshold,
                    market_cap_threshold=args.min_market_cap,
                    standalone_html=standalone_html,
                    standalone_error=standalone_error,
                )
            status = "PASS" if result.passed else "FAIL"
            if result.passed:
                print(f"[{index}/{len(stocks)}] {stock.symbol}: {status}", flush=True)
            elif args.fail_format == "inline":
                detail = fail_detail(
                    result,
                    args.sales_growth,
                    args.return_threshold,
                )
                print(f"[{index}/{len(stocks)}] {stock.symbol}: {status}{detail}", flush=True)
            else:
                print(f"[{index}/{len(stocks)}] {stock.symbol}: {status}", flush=True)
                print_fail_table(
                    result,
                    args.sales_growth,
                    args.return_threshold,
                    args.min_market_cap,
                )
            if result.passed:
                passed_symbols.append(result.symbol)
                print(
                    "Passed so far: " + ", ".join(passed_symbols),
                    flush=True,
                )
            results.append(result)
        except Exception as exc:
            reason = str(exc)
            print(
                f"[{index}/{len(stocks)}] {stock.symbol}: ERROR - {reason}",
                flush=True,
            )
            failed_check = SeriesCheck(False, reason)
            results.append(
                StockResult(
                    symbol=stock.symbol,
                    company_name=stock.company_name,
                    industry=stock.industry,
                    is_financial=False,
                    metric_name="ROE",
                    market_cap_cr=None,
                    market_cap_passed=False,
                    market_cap_reason=reason,
                    passed=False,
                    sales_check=failed_check,
                    metric_check=failed_check,
                )
            )

        if args.sleep > 0 and index < len(stocks):
            time.sleep(args.sleep)

    return results


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Filter NSE stocks where last 10 YoY sales/revenue growth rates are "
            ">=10% and annual ROE/ROCE values are >=15%."
        )
    )
    parser.add_argument(
        "--csv-file",
        nargs="+",
        required=True,
        help="One or more input CSV files with Symbol or Ticker column.",
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--passed-txt", type=Path, default=DEFAULT_PASSED_TXT)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_OUTPUT_MD)
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--sales-growth", type=float, default=10.0)
    parser.add_argument("--return-threshold", type=float, default=15.0)
    parser.add_argument(
        "--min-market-cap",
        type=float,
        default=DEFAULT_MIN_MARKET_CAP_CR,
        help="Minimum market cap in crore. Default: 100.",
    )
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=30,
        help="Refresh Screener HTML if older than this many days. Use -1 to never refresh existing files.",
    )
    parser.add_argument("--no-fetch", action="store_true", help="Use only existing local Screener HTML.")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--sleep", type=float, default=1.0, help="Delay between Screener requests.")
    parser.add_argument(
        "--fail-format",
        choices=("table", "inline"),
        default="table",
        help="How to print failed stock details in terminal. Default: table.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    results = process_stocks(args)
    results.sort(key=lambda item: (not item.passed, item.symbol))

    write_csv(results, args.output_csv)
    write_markdown(results, args.output_md)
    write_passed_txt(results, args.passed_txt)

    passed_count = sum(1 for result in results if result.passed)
    print_passed_summary(results)
    print(f"\nMatched stocks: {passed_count} / {len(results)}")
    print(f"Passed tickers output: {args.passed_txt}")
    print(f"CSV output: {args.output_csv}")
    print(f"Markdown output: {args.output_md}")


if __name__ == "__main__":
    main()
