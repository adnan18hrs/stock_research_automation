#!/usr/bin/env python3
"""
Backtest a yearly portfolio using the annual technical + fundamental filter set.

The portfolio mechanics intentionally mirror backtest_rsi_multiframe_screener.py:
build a portfolio every 15-May, invest equal money per selected stock, keep holding it,
measure current value, portfolio CAGR to the valuation date, yearly summaries, and NIFTY 50 alpha.

Point-in-time fundamentals are approximated from locally saved Screener tables:
- annual rows use the fiscal year ended 31-Mar before the 15-May formation date
- shareholding rows are usable only after --shareholding-lag-days

Default run:
    python3 scripts/backtest_multifilter_screener.py

Faster smoke test:
    python3 scripts/backtest_multifilter_screener.py --max-symbols 100 --end-date 2023-06-01
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

import pandas as pd

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - friendly runtime message
    yf = None

try:
    import requests
except ImportError:  # pragma: no cover - optional live Screener fetch
    requests = None

from backtest_rsi_multiframe_screener import (
    BacktestRow,
    NIFTY_50_SYMBOL,
    PROJECT_ROOT,
    batched,
    build_combined_summary,
    build_yearly_summary,
    cagr_pct,
    first_price_on_or_after,
    fmt_pct,
    format_date,
    has_required_prior_history,
    last_price_on_or_before,
    load_symbols,
    nifty_return_until_pct,
    normalize_price_series,
    print_current_summary,
)


DEFAULT_SYMBOLS_FILE = PROJECT_ROOT / "config" / "Ticker_List_NSE_India_2000.csv"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_OUTPUT_CSV = PROJECT_ROOT / "data" / "multifilter_backtest_results.csv"
DEFAULT_TRADES_CSV = PROJECT_ROOT / "data" / "multifilter_backtest_trades.csv"
DEFAULT_YEARLY_SUMMARY_CSV = PROJECT_ROOT / "data" / "multifilter_yearly_summary.csv"
DEFAULT_POST_JULY_2025_SUMMARY_CSV = PROJECT_ROOT / "data" / "multifilter_post_july_2025_summary.csv"
DEFAULT_POST_JULY_2025_START = "2025-07-01"
DEFAULT_REQUIRED_HISTORY_MONTHS = 14
DEFAULT_MIN_HISTORY_TRADING_DAYS = 252
YEARLY_FORMATION_MONTH = 5
YEARLY_FORMATION_DAY = 15
SCREENER_TABLE_IDS = {
    "quarters",
    "profit-loss",
    "balance-sheet",
    "cash-flow",
    "ratios",
    "shareholding",
}
MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
PERIOD_END_DAY = {3: 31, 6: 30, 9: 30, 12: 31}


@dataclass(frozen=True)
class MarketData:
    close: dict[str, pd.Series]
    adjusted: dict[str, pd.Series]


@dataclass(frozen=True)
class FundamentalSnapshot:
    latest_year: date
    sales: float
    sales_yoy_growth_pct: float
    profit: float
    profit_yoy_growth_pct: float
    profit_previous_year: float
    profit_two_years_back: float
    profit_three_years_back: float
    opm_pct: float
    sales_growth_3y_pct: float
    profit_growth_3y_pct: float
    debt_to_equity: float
    promoter_holding_pct: float
    pledged_percentage: float
    pledged_source: str
    roce_pct: float
    roe_pct: float
    interest_coverage_ratio: float
    peg_ratio: float
    market_cap_cr: float
    pe_ratio: float
    annual_eps: float


@dataclass(frozen=True)
class FilterSnapshot:
    trade_date: pd.Timestamp
    current_price: float
    fundamentals: FundamentalSnapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest yearly portfolios on 15-May using the annual multifilter set."
    )
    parser.add_argument("--symbols-file", type=Path, default=DEFAULT_SYMBOLS_FILE)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument(
        "--end-date",
        default=None,
        help="Last portfolio formation date boundary. Default is today.",
    )
    parser.add_argument("--valuation-date", default=None, help="Current/live valuation date. Default is today.")
    parser.add_argument("--investment-per-stock", type=float, default=100.0)
    parser.add_argument(
        "--min-stocks",
        type=int,
        default=1,
        help="Minimum passed stocks required to invest. Default is 1 so every year with any passed stock is shown.",
    )
    parser.add_argument("--batch-size", type=int, default=80)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--download-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--required-history-months", type=int, default=DEFAULT_REQUIRED_HISTORY_MONTHS)
    parser.add_argument("--min-history-trading-days", type=int, default=DEFAULT_MIN_HISTORY_TRADING_DAYS)
    parser.add_argument(
        "--annual-report-lag-days",
        type=int,
        default=0,
        help="Kept for compatibility; annual rows are selected by fiscal year ended 31-Mar.",
    )
    parser.add_argument("--shareholding-lag-days", type=int, default=30)
    parser.add_argument(
        "--assume-zero-pledge-when-missing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Screener free HTML usually omits pledge rows. Default treats missing pledge as 0 and records "
            "pledged_source=assumed_missing. Use --no-assume-zero-pledge-when-missing to require explicit data."
        ),
    )
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--trades-csv", type=Path, default=DEFAULT_TRADES_CSV)
    parser.add_argument("--yearly-summary-csv", type=Path, default=DEFAULT_YEARLY_SUMMARY_CSV)
    parser.add_argument("--post-july-2025-summary-csv", type=Path, default=DEFAULT_POST_JULY_2025_SUMMARY_CSV)
    parser.add_argument("--post-july-2025-start", default=DEFAULT_POST_JULY_2025_START)
    parser.add_argument(
        "--fetch-missing-screener",
        action="store_true",
        help="Fetch missing Screener company pages live and cache them under --data-dir.",
    )
    parser.add_argument("--include-non-eq", action="store_true")
    return parser.parse_args()


def yearly_formation_dates(start: date, end: date) -> list[date]:
    dates: list[date] = []
    for year in range(start.year, end.year + 1):
        candidate = date(year, YEARLY_FORMATION_MONTH, YEARLY_FORMATION_DAY)
        if start <= candidate <= end:
            dates.append(candidate)
    return dates


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def parse_number(value: object) -> float | None:
    if value is None:
        return None
    text = clean_text(str(value))
    if not text or text in {"-", "--", "N/A"}:
        return None
    text = text.replace("%", "").replace(",", "").replace("₹", "").replace("Cr.", "").replace("Cr", "")
    text = text.replace("−", "-")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def normalized_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def parse_period_label(label: str) -> date | None:
    match = re.fullmatch(r"([A-Za-z]{3})\s+(\d{4})", clean_text(label))
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    year = int(match.group(2))
    day = PERIOD_END_DAY.get(month, 28)
    return date(year, month, day)


def pct_growth(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior <= 0:
        return None
    return ((current / prior) - 1) * 100


def cagr_from_values(start_value: float | None, end_value: float | None, years: float) -> float | None:
    if start_value is None or end_value is None or start_value <= 0 or end_value <= 0 or years <= 0:
        return None
    return ((end_value / start_value) ** (1 / years) - 1) * 100


class ScreenerHTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: dict[str, list[list[str]]] = {}
        self.top_ratios: dict[str, str] = {}
        self._section_stack: list[tuple[str, str]] = []
        self._collecting_table = False
        self._table_section = ""
        self._current_row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._top_ratios_depth = 0
        self._ratio_li_depth = 0
        self._ratio_name_parts: list[str] = []
        self._ratio_number_parts: list[str] = []
        self._ratio_target: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        element_id = attr.get("id", "")

        if element_id in SCREENER_TABLE_IDS:
            self._section_stack.append((tag, element_id))

        if tag == "ul" and element_id == "top-ratios":
            self._top_ratios_depth = 1
        elif self._top_ratios_depth:
            self._top_ratios_depth += 1

        if self._top_ratios_depth and tag == "li":
            self._ratio_li_depth = 1
            self._ratio_name_parts = []
            self._ratio_number_parts = []
        elif self._ratio_li_depth:
            self._ratio_li_depth += 1

        if self._ratio_li_depth and tag == "span":
            class_attr = attr.get("class", "")
            if "name" in class_attr.split():
                self._ratio_target = "name"
            elif "number" in class_attr.split():
                self._ratio_target = "number"

        current_section = self._section_stack[-1][1] if self._section_stack else ""
        if tag == "table" and current_section:
            self._collecting_table = True
            self._table_section = current_section

        if self._collecting_table and tag == "tr":
            self._current_row = []
        if self._collecting_table and tag in {"th", "td"}:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        if self._ratio_target == "name":
            self._ratio_name_parts.append(data)
        elif self._ratio_target == "number":
            self._ratio_number_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._collecting_table and tag in {"th", "td"} and self._cell_parts is not None:
            if self._current_row is not None:
                self._current_row.append(clean_text(" ".join(self._cell_parts)))
            self._cell_parts = None

        if self._collecting_table and tag == "tr" and self._current_row is not None:
            if any(self._current_row):
                self.tables.setdefault(self._table_section, []).append(self._current_row)
            self._current_row = None

        if self._collecting_table and tag == "table":
            self._collecting_table = False
            self._table_section = ""

        if tag == "span" and self._ratio_target:
            self._ratio_target = None

        if self._ratio_li_depth:
            if tag == "li":
                name = clean_text(" ".join(self._ratio_name_parts))
                value = clean_text(" ".join(self._ratio_number_parts))
                if name and value:
                    self.top_ratios[name] = value
                self._ratio_li_depth = 0
                self._ratio_target = None
            else:
                self._ratio_li_depth -= 1

        if self._top_ratios_depth:
            if tag == "ul" and self._top_ratios_depth == 1:
                self._top_ratios_depth = 0
            elif tag != "li":
                self._top_ratios_depth -= 1

        if self._section_stack and self._section_stack[-1][0] == tag:
            self._section_stack.pop()


def base_symbol(yahoo_symbol: str) -> str:
    return yahoo_symbol.removesuffix(".NS")


def parse_screener_html(ticker: str, html: str) -> dict[str, object]:
    parser = ScreenerHTMLTableParser()
    parser.feed(html)
    return {"ticker": ticker, "top_ratios": parser.top_ratios, "tables": parser.tables}


def fetch_screener_finance(symbol: str, data_dir: Path, timeout: float = 20.0) -> dict[str, object] | None:
    if requests is None:
        return None
    ticker = base_symbol(symbol)
    url = f"https://www.screener.in/company/{ticker}/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    if response.status_code != 200 or not response.text:
        return None

    screener_dir = data_dir / ticker / "screener_finance"
    screener_dir.mkdir(parents=True, exist_ok=True)
    html_path = screener_dir / "company_page.html"
    cache_path = screener_dir / "screener_finance_cache.json"
    html_path.write_text(response.text, encoding="utf-8")
    finance = parse_screener_html(ticker, response.text)
    cache_path.write_text(json.dumps(finance), encoding="utf-8")
    return finance


def load_screener_finance(
    symbol: str,
    data_dir: Path,
    fetch_missing: bool = False,
) -> dict[str, object] | None:
    ticker = base_symbol(symbol)
    screener_dir = data_dir / ticker / "screener_finance"
    cache_path = screener_dir / "screener_finance_cache.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    html_path = screener_dir / "company_page.html"
    if not html_path.exists():
        return fetch_screener_finance(symbol, data_dir) if fetch_missing else None

    finance = parse_screener_html(ticker, html_path.read_text(encoding="utf-8", errors="ignore"))
    if fetch_missing:
        try:
            cache_path.write_text(json.dumps(finance), encoding="utf-8")
        except OSError:
            pass
    return finance


def row_series(table: list[list[str]], label_candidates: Iterable[str]) -> list[tuple[date, float]]:
    wanted = {normalized_label(label) for label in label_candidates}
    header_dates: list[date | None] = []
    values: list[tuple[date, float]] = []

    for row in table:
        if not row:
            continue
        candidate_dates = [parse_period_label(cell) for cell in row[1:]]
        if any(candidate_dates):
            header_dates = candidate_dates
            continue
        if normalized_label(row[0]) not in wanted or not header_dates:
            continue
        for period, cell in zip(header_dates, row[1:]):
            number = parse_number(cell)
            if period is not None and number is not None:
                values.append((period, number))

    return sorted(values, key=lambda item: item[0])


def latest_rows_as_of(
    series_by_name: dict[str, list[tuple[date, float]]],
    as_of: date,
    lag_days: int,
) -> list[tuple[date, dict[str, float]]]:
    periods = sorted({period for rows in series_by_name.values() for period, _ in rows})
    usable_periods = [period for period in periods if period + timedelta(days=lag_days) <= as_of]
    combined: list[tuple[date, dict[str, float]]] = []
    for period in usable_periods:
        row: dict[str, float] = {}
        for name, rows in series_by_name.items():
            values = {row_period: value for row_period, value in rows}
            if period in values:
                row[name] = values[period]
        combined.append((period, row))
    return combined


def closest_annual_at_or_before(rows: list[tuple[date, dict[str, float]]], target: date) -> tuple[date, dict[str, float]] | None:
    usable = [row for row in rows if row[0] <= target]
    return usable[-1] if usable else None


def fiscal_year_end_for_signal(as_of: date) -> date:
    year = as_of.year if (as_of.month, as_of.day) >= (4, 1) else as_of.year - 1
    return date(year, 3, 31)


def latest_shareholding_as_of(
    table: list[list[str]],
    as_of: date,
    lag_days: int,
    assume_zero_pledge_when_missing: bool,
) -> tuple[float, float, str] | None:
    promoter_series = row_series(table, ["Promoters +", "Promoters"])
    pledged_series = row_series(table, ["Pledged", "Pledged %", "Promoter Pledging %", "Shares Pledged"])
    usable_promoters = [
        (period, value) for period, value in promoter_series if period + timedelta(days=lag_days) <= as_of
    ]
    if not usable_promoters:
        return None
    period, promoter_value = usable_promoters[-1]
    pledged_map = {row_period: value for row_period, value in pledged_series}
    if period in pledged_map:
        return promoter_value, pledged_map[period], "screener"
    if assume_zero_pledge_when_missing:
        return promoter_value, 0.0, "assumed_missing"
    return None


def latest_annual_roce(rows: list[tuple[date, dict[str, float]]]) -> float | None:
    if not rows:
        return None
    return rows[-1][1].get("roce")


def compute_roe(rows: list[tuple[date, dict[str, float]]]) -> float | None:
    if not rows:
        return None
    latest_period, latest = rows[-1]
    latest_equity = latest.get("equity_capital", 0.0) + latest.get("reserves", 0.0)
    previous = closest_annual_at_or_before(rows, date(latest_period.year - 1, latest_period.month, latest_period.day))
    if previous is not None:
        prior = previous[1]
        prior_equity = prior.get("equity_capital", 0.0) + prior.get("reserves", 0.0)
        equity = (latest_equity + prior_equity) / 2 if prior_equity > 0 else latest_equity
    else:
        equity = latest_equity
    net_profit = latest.get("net_profit")
    if net_profit is None or equity <= 0:
        return None
    return net_profit / equity * 100


def build_fundamental_snapshot(
    symbol: str,
    as_of: date,
    trade_price: float,
    finance: dict[str, object] | None,
    annual_lag_days: int,
    shareholding_lag_days: int,
    assume_zero_pledge_when_missing: bool,
) -> FundamentalSnapshot | None:
    if not finance:
        return None
    tables = finance.get("tables", {})
    top_ratios = finance.get("top_ratios", {})
    if not isinstance(tables, dict) or not isinstance(top_ratios, dict):
        return None

    profit_loss_table = tables.get("profit-loss", [])
    balance_sheet_table = tables.get("balance-sheet", [])
    ratios_table = tables.get("ratios", [])
    shareholding_table = tables.get("shareholding", [])

    target_fy_end = fiscal_year_end_for_signal(as_of)
    annual_rows = latest_rows_as_of(
        {
            "sales": row_series(profit_loss_table, ["Sales +", "Sales"]),
            "operating_profit": row_series(profit_loss_table, ["Operating Profit"]),
            "interest": row_series(profit_loss_table, ["Interest"]),
            "net_profit": row_series(profit_loss_table, ["Net Profit +", "Net Profit"]),
            "opm": row_series(profit_loss_table, ["OPM %"]),
            "eps": row_series(profit_loss_table, ["EPS in Rs"]),
            "equity_capital": row_series(balance_sheet_table, ["Equity Capital"]),
            "reserves": row_series(balance_sheet_table, ["Reserves"]),
            "borrowings": row_series(balance_sheet_table, ["Borrowings +", "Borrowings"]),
            "roce": row_series(ratios_table, ["ROCE %", "ROCE"]),
        },
        as_of=target_fy_end,
        lag_days=0,
    )
    annual_by_year = {period.year: (period, values) for period, values in annual_rows}
    required_years = [target_fy_end.year, target_fy_end.year - 1, target_fy_end.year - 2, target_fy_end.year - 3]
    if any(year not in annual_by_year for year in required_years):
        return None

    latest_a, latest_annual = annual_by_year[target_fy_end.year]
    _, previous_annual = annual_by_year[target_fy_end.year - 1]
    _, two_years_back_annual = annual_by_year[target_fy_end.year - 2]
    _, three_years_back_annual = annual_by_year[target_fy_end.year - 3]

    sales_yoy = pct_growth(latest_annual.get("sales"), previous_annual.get("sales"))
    profit_yoy = pct_growth(latest_annual.get("net_profit"), previous_annual.get("net_profit"))
    sales_growth_3y = cagr_from_values(three_years_back_annual.get("sales"), latest_annual.get("sales"), 3)
    profit_growth_3y = cagr_from_values(three_years_back_annual.get("net_profit"), latest_annual.get("net_profit"), 3)
    if any(value is None for value in (sales_yoy, profit_yoy, sales_growth_3y, profit_growth_3y)):
        return None

    equity = latest_annual.get("equity_capital", 0.0) + latest_annual.get("reserves", 0.0)
    borrowings = latest_annual.get("borrowings")
    if borrowings is None or equity <= 0:
        return None
    debt_to_equity = borrowings / equity

    shareholding = latest_shareholding_as_of(
        shareholding_table,
        as_of=as_of,
        lag_days=shareholding_lag_days,
        assume_zero_pledge_when_missing=assume_zero_pledge_when_missing,
    )
    if shareholding is None:
        return None
    promoter_holding, pledged_percentage, pledged_source = shareholding

    annual_rows_through_latest = [row for row in annual_rows if row[0] <= latest_a]
    roce = latest_annual_roce(annual_rows_through_latest)
    roe = compute_roe(annual_rows_through_latest)
    if roce is None or roe is None:
        return None

    annual_eps = latest_annual.get("eps")
    if annual_eps is None or annual_eps <= 0:
        return None
    pe_ratio = trade_price / annual_eps
    if profit_growth_3y <= 0:
        return None
    peg_ratio = pe_ratio / profit_growth_3y

    operating_profit = latest_annual.get("operating_profit")
    interest = latest_annual.get("interest")
    if operating_profit is None or interest is None:
        return None
    if interest <= 0:
        interest_coverage_ratio = math.inf if operating_profit > 0 else None
    else:
        interest_coverage_ratio = operating_profit / interest
    if interest_coverage_ratio is None:
        return None

    face_value = parse_number(top_ratios.get("Face Value"))
    equity_capital = latest_annual.get("equity_capital")
    if face_value is None or face_value <= 0 or equity_capital is None:
        return None
    market_cap_cr = trade_price * (equity_capital / face_value)

    required_values = {
        "sales": latest_annual.get("sales"),
        "profit": latest_annual.get("net_profit"),
        "previous_profit": previous_annual.get("net_profit"),
        "two_back_profit": two_years_back_annual.get("net_profit"),
        "three_back_profit": three_years_back_annual.get("net_profit"),
        "opm": latest_annual.get("opm"),
    }
    if any(value is None for value in required_values.values()):
        return None

    return FundamentalSnapshot(
        latest_year=latest_a,
        sales=float(required_values["sales"]),
        sales_yoy_growth_pct=sales_yoy,
        profit=float(required_values["profit"]),
        profit_yoy_growth_pct=profit_yoy,
        profit_previous_year=float(required_values["previous_profit"]),
        profit_two_years_back=float(required_values["two_back_profit"]),
        profit_three_years_back=float(required_values["three_back_profit"]),
        opm_pct=float(required_values["opm"]),
        sales_growth_3y_pct=sales_growth_3y,
        profit_growth_3y_pct=profit_growth_3y,
        debt_to_equity=debt_to_equity,
        promoter_holding_pct=promoter_holding,
        pledged_percentage=pledged_percentage,
        pledged_source=pledged_source,
        roce_pct=roce,
        roe_pct=roe,
        interest_coverage_ratio=interest_coverage_ratio,
        peg_ratio=peg_ratio,
        market_cap_cr=market_cap_cr,
        pe_ratio=pe_ratio,
        annual_eps=annual_eps,
    )


def passes_full_filter(
    symbol: str,
    close: pd.Series,
    as_of: date,
    finance: dict[str, object] | None,
    annual_lag_days: int,
    shareholding_lag_days: int,
    assume_zero_pledge_when_missing: bool,
) -> FilterSnapshot | None:
    entry = first_price_on_or_after(close, as_of)
    if entry is None:
        return None
    trade_date, current_price = entry

    fundamentals = build_fundamental_snapshot(
        symbol=symbol,
        as_of=as_of,
        trade_price=current_price,
        finance=finance,
        annual_lag_days=annual_lag_days,
        shareholding_lag_days=shareholding_lag_days,
        assume_zero_pledge_when_missing=assume_zero_pledge_when_missing,
    )
    if fundamentals is None:
        return None

    if final_filter_failures(fundamentals):
        return None

    return FilterSnapshot(
        trade_date=trade_date,
        current_price=current_price,
        fundamentals=fundamentals,
    )


def final_filter_failures(fundamentals: FundamentalSnapshot) -> list[str]:
    checks = [
        ("sales_growth_3y<=15", fundamentals.sales_growth_3y_pct > 15),
        ("profit_growth_3y<=20", fundamentals.profit_growth_3y_pct > 20),
        ("annual_sales_yoy<=20", fundamentals.sales_yoy_growth_pct > 20),
        ("annual_profit_yoy<=25", fundamentals.profit_yoy_growth_pct > 25),
        ("roe<=15", fundamentals.roe_pct > 15),
        ("roce<=15", fundamentals.roce_pct > 15),
        ("debt_to_equity>=1", fundamentals.debt_to_equity < 1),
        ("opm<=15", fundamentals.opm_pct > 15),
        ("interest_coverage<=4", fundamentals.interest_coverage_ratio > 4),
        ("promoter_holding<=45", fundamentals.promoter_holding_pct > 45),
        ("pledged_percentage!=0", fundamentals.pledged_percentage == 0),
        ("market_cap<=1000", fundamentals.market_cap_cr > 1000),
        ("peg>=3", fundamentals.peg_ratio < 3),
        ("profit_not_above_previous_year", fundamentals.profit > fundamentals.profit_previous_year),
        ("profit_not_above_2y_back", fundamentals.profit > fundamentals.profit_two_years_back),
        ("profit_not_above_3y_back", fundamentals.profit > fundamentals.profit_three_years_back),
    ]
    return [label for label, passed in checks if not passed]


def download_market_data(
    symbols: list[str],
    start: date,
    end: date,
    batch_size: int,
    sleep_seconds: float,
    timeout_seconds: float,
) -> MarketData:
    if yf is None:
        raise RuntimeError("yfinance is not installed. Run: pip install yfinance")

    close_by_symbol: dict[str, pd.Series] = {}
    adjusted_by_symbol: dict[str, pd.Series] = {}
    batches = list(batched(symbols, batch_size))

    for batch_number, batch in enumerate(batches, start=1):
        print(f"Downloading batch {batch_number}/{len(batches)} ({len(batch)} symbols)...", flush=True)
        data = yf.download(
            tickers=batch,
            start=start.isoformat(),
            end=end.isoformat(),
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=False,
            timeout=timeout_seconds,
        )

        for symbol in batch:
            try:
                symbol_frame = data[symbol].copy() if isinstance(data.columns, pd.MultiIndex) else data.copy()
                if symbol_frame.empty or "Close" not in symbol_frame.columns:
                    continue
                close = normalize_price_series(symbol_frame["Close"])
                adjusted = normalize_price_series(
                    symbol_frame["Adj Close"] if "Adj Close" in symbol_frame.columns else symbol_frame["Close"]
                )
                if not close.empty:
                    close_by_symbol[symbol] = close
                    adjusted_by_symbol[symbol] = adjusted
            except Exception as exc:  # noqa: BLE001 - keep the backtest moving per symbol
                print(f"Skipping {symbol}: {exc}")

        if sleep_seconds and batch_number < len(batches):
            time.sleep(sleep_seconds)

    return MarketData(close=close_by_symbol, adjusted=adjusted_by_symbol)


def trade_snapshot_record(snapshot: FilterSnapshot) -> dict[str, object]:
    f = snapshot.fundamentals
    return {
        "current_price_signal": round(snapshot.current_price, 4),
        "latest_year_used": f.latest_year.isoformat(),
        "annual_sales": round(f.sales, 4),
        "annual_sales_yoy_growth_pct": round(f.sales_yoy_growth_pct, 4),
        "annual_net_profit": round(f.profit, 4),
        "net_profit_preceding_year": round(f.profit_previous_year, 4),
        "net_profit_2years_back": round(f.profit_two_years_back, 4),
        "net_profit_3years_back": round(f.profit_three_years_back, 4),
        "opm_pct": round(f.opm_pct, 4),
        "sales_growth_3y_pct": round(f.sales_growth_3y_pct, 4),
        "profit_growth_3y_pct": round(f.profit_growth_3y_pct, 4),
        "debt_to_equity": round(f.debt_to_equity, 4),
        "promoter_holding_pct": round(f.promoter_holding_pct, 4),
        "pledged_percentage": round(f.pledged_percentage, 4),
        "pledged_source": f.pledged_source,
        "roce_pct": round(f.roce_pct, 4),
        "roe_pct": round(f.roe_pct, 4),
        "interest_coverage_ratio": round(f.interest_coverage_ratio, 4),
        "pe_ratio": round(f.pe_ratio, 4),
        "annual_eps": round(f.annual_eps, 4),
        "peg_ratio": round(f.peg_ratio, 4),
        "market_cap_cr": round(f.market_cap_cr, 4),
    }


def print_filter_month_result(row: BacktestRow, valuation_date: date) -> None:
    heading = (
        f"{format_date(row.start_date)} "
        f"(eligible {row.history_eligible_count}, excluded no 14m data "
        f"{row.insufficient_history_count}, full-filter passed {row.passed_count})"
    )
    if row.skipped_reason:
        print(f"{heading}: skipped - {row.skipped_reason}")
        return

    current_text = (
        f"buy-and-hold from {format_date(row.start_date)} till {format_date(valuation_date)}: "
        f"portfolio total return {fmt_pct(row.current_return_pct)}; "
        f"portfolio CAGR {fmt_pct(row.current_cagr_pct)}; "
        f"NIFTY 50 total return {fmt_pct(row.current_nifty_return_pct)}; "
        f"NIFTY 50 CAGR {fmt_pct(row.current_nifty_cagr_pct)}; "
        f"current alpha {fmt_pct(row.current_alpha_pct)}"
    )
    print(f"{heading}: {current_text}.")


def csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isinf(value):
            return "inf"
        return f"{value:.4f}"
    return str(value)


def stock_value_map(stock_records: list[dict[str, object]], key: str) -> str:
    return "; ".join(f"{record['symbol']}:{csv_value(record.get(key))}" for record in stock_records)


def build_portfolio_record(
    row: BacktestRow,
    stock_records: list[dict[str, object]],
    investment_per_stock: float,
    valuation_date: date,
) -> dict[str, object]:
    invested_amount = investment_per_stock * row.invested_count
    valued_invested_amount = investment_per_stock * row.current_valued_count
    alpha_cagr = (
        None
        if row.current_cagr_pct is None or row.current_nifty_cagr_pct is None
        else row.current_cagr_pct - row.current_nifty_cagr_pct
    )
    return {
        "portfolio_start_date": row.start_date.isoformat(),
        "valuation_date": valuation_date.isoformat(),
        "status": "skipped" if row.skipped_reason else "selected",
        "skipped_reason": row.skipped_reason,
        "history_eligible_count": row.history_eligible_count,
        "insufficient_14m_history_count": row.insufficient_history_count,
        "passed_count": row.passed_count,
        "stocks": ", ".join(record["symbol"] for record in stock_records),
        "stock_count": row.invested_count,
        "current_valued_count": row.current_valued_count,
        "investment_per_stock": round(investment_per_stock, 2),
        "invested_amount": round(invested_amount, 2),
        "valued_invested_amount": round(valued_invested_amount, 2),
        "current_value": round(row.current_value, 2),
        "portfolio_total_return_pct": None if row.current_return_pct is None else round(row.current_return_pct, 4),
        "portfolio_cagr_pct": None if row.current_cagr_pct is None else round(row.current_cagr_pct, 4),
        "nifty50_total_return_pct": (
            None if row.current_nifty_return_pct is None else round(row.current_nifty_return_pct, 4)
        ),
        "nifty50_cagr_pct": None if row.current_nifty_cagr_pct is None else round(row.current_nifty_cagr_pct, 4),
        "alpha_total_return_pct": None if row.current_alpha_pct is None else round(row.current_alpha_pct, 4),
        "alpha_cagr_pct": None if alpha_cagr is None else round(alpha_cagr, 4),
        "entry_trade_dates": stock_value_map(stock_records, "entry_trade_date"),
        "entry_prices": stock_value_map(stock_records, "entry_price"),
        "quantities": stock_value_map(stock_records, "quantity"),
        "current_prices": stock_value_map(stock_records, "current_price"),
        "current_values": stock_value_map(stock_records, "current_value"),
        "stock_total_return_pct": stock_value_map(stock_records, "current_return_pct"),
        "stock_cagr_pct": stock_value_map(stock_records, "current_cagr_pct"),
        "latest_year_used": stock_value_map(stock_records, "latest_year_used"),
        "annual_net_profit": stock_value_map(stock_records, "annual_net_profit"),
        "annual_sales": stock_value_map(stock_records, "annual_sales"),
        "sales_growth_3y_pct": stock_value_map(stock_records, "sales_growth_3y_pct"),
        "profit_growth_3y_pct": stock_value_map(stock_records, "profit_growth_3y_pct"),
        "roe_pct": stock_value_map(stock_records, "roe_pct"),
        "roce_pct": stock_value_map(stock_records, "roce_pct"),
        "debt_to_equity": stock_value_map(stock_records, "debt_to_equity"),
        "opm_pct": stock_value_map(stock_records, "opm_pct"),
        "interest_coverage_ratio": stock_value_map(stock_records, "interest_coverage_ratio"),
        "promoter_holding_pct": stock_value_map(stock_records, "promoter_holding_pct"),
        "pledged_percentage": stock_value_map(stock_records, "pledged_percentage"),
        "market_cap_cr": stock_value_map(stock_records, "market_cap_cr"),
        "peg_ratio": stock_value_map(stock_records, "peg_ratio"),
    }


def run_backtest(
    symbols: list[str],
    market_data: MarketData,
    finance_by_symbol: dict[str, dict[str, object] | None],
    formation_dates: list[date],
    investment_per_stock: float,
    min_stocks: int,
    valuation_date: date,
    required_history_months: int,
    min_history_trading_days: int,
    annual_lag_days: int,
    shareholding_lag_days: int,
    assume_zero_pledge_when_missing: bool,
) -> tuple[list[BacktestRow], list[dict[str, object]]]:
    rows: list[BacktestRow] = []
    portfolio_records: list[dict[str, object]] = []
    nifty_prices = market_data.adjusted.get(NIFTY_50_SYMBOL)

    for as_of in formation_dates:
        holding_end_date = valuation_date
        history_eligible = [
            symbol
            for symbol in symbols
            if symbol in market_data.close
            and has_required_prior_history(
                market_data.close[symbol],
                as_of,
                required_history_months=required_history_months,
                min_history_trading_days=min_history_trading_days,
            )
        ]
        insufficient_history_count = len(symbols) - len(history_eligible)

        snapshots: dict[str, FilterSnapshot] = {}
        missing_finance_count = 0
        missing_entry_count = 0
        unusable_fundamentals_count = 0
        filter_failure_counts: dict[str, int] = {}
        for symbol in history_eligible:
            finance = finance_by_symbol.get(symbol)
            if finance is None:
                missing_finance_count += 1
                continue

            entry = first_price_on_or_after(market_data.close[symbol], as_of)
            if entry is None:
                missing_entry_count += 1
                continue
            trade_date, current_price = entry

            fundamentals = build_fundamental_snapshot(
                symbol=symbol,
                as_of=as_of,
                trade_price=current_price,
                finance=finance,
                annual_lag_days=annual_lag_days,
                shareholding_lag_days=shareholding_lag_days,
                assume_zero_pledge_when_missing=assume_zero_pledge_when_missing,
            )
            if fundamentals is None:
                unusable_fundamentals_count += 1
                continue

            failures = final_filter_failures(fundamentals)
            if failures:
                for failure in failures:
                    filter_failure_counts[failure] = filter_failure_counts.get(failure, 0) + 1
                continue

            snapshots[symbol] = FilterSnapshot(
                trade_date=trade_date,
                current_price=current_price,
                fundamentals=fundamentals,
            )

        passed = list(snapshots)
        if len(passed) < min_stocks:
            top_failures = ", ".join(
                f"{label}:{count}"
                for label, count in sorted(filter_failure_counts.items(), key=lambda item: item[1], reverse=True)[:5]
            )
            rows.append(
                BacktestRow(
                    start_date=as_of,
                    exit_date=holding_end_date,
                    history_eligible_count=len(history_eligible),
                    insufficient_history_count=insufficient_history_count,
                    passed_count=len(passed),
                    invested_count=0,
                    initial_value=0.0,
                    final_value=0.0,
                    portfolio_return_pct=None,
                    nifty_return_pct=None,
                    alpha_pct=None,
                    current_valued_count=0,
                    current_value=0.0,
                    current_return_pct=None,
                    current_cagr_pct=None,
                    current_nifty_value=None,
                    current_nifty_return_pct=None,
                    current_nifty_cagr_pct=None,
                    current_alpha_pct=None,
                    stocks=passed,
                    skipped_reason=(
                        f"{len(passed)} stocks passed full filter; minimum is {min_stocks}. "
                        f"Eligible {len(history_eligible)}; missing Screener finance {missing_finance_count}; "
                        f"missing entry price {missing_entry_count}; unusable fundamentals {unusable_fundamentals_count}; "
                        f"top filter failures: {top_failures or 'none'}."
                    ),
                )
            )
            portfolio_records.append(
                build_portfolio_record(
                    row=rows[-1],
                    stock_records=[],
                    investment_per_stock=investment_per_stock,
                    valuation_date=valuation_date,
                )
            )
            print_filter_month_result(rows[-1], valuation_date)
            continue

        current_value = 0.0
        current_valued_count = 0
        invested_count = 0
        stock_records: list[dict[str, object]] = []
        for symbol in passed:
            adjusted_close = market_data.adjusted.get(symbol, pd.Series(dtype=float))
            entry = first_price_on_or_after(adjusted_close, as_of)
            if entry is None:
                continue
            entry_date, entry_price = entry
            if entry_price <= 0:
                continue

            snapshot = snapshots[symbol]
            invested_count += 1
            quantity = investment_per_stock / entry_price

            current_trade_date = None
            current_price = None
            current_trade_value = None
            current_return_pct = None
            current_cagr = None
            current = last_price_on_or_before(adjusted_close, valuation_date)
            if current is not None:
                current_trade_date, current_price = current
                current_trade_value = quantity * current_price
                current_value += current_trade_value
                current_valued_count += 1
                current_return_pct = ((current_price / entry_price) - 1) * 100
                current_cagr = cagr_pct(as_of, valuation_date, current_return_pct)

            stock_snapshot = trade_snapshot_record(snapshot)
            stock_records.append(
                {
                    "symbol": symbol,
                    "entry_trade_date": entry_date.date().isoformat(),
                    "entry_price": round(entry_price, 4),
                    "quantity": round(quantity, 8),
                    **stock_snapshot,
                    "current_valuation_date": current_trade_date.date().isoformat() if current_trade_date is not None else "",
                    "current_price": None if current_price is None else round(current_price, 4),
                    "current_value": None if current_trade_value is None else round(current_trade_value, 2),
                    "current_return_pct": None if current_return_pct is None else round(current_return_pct, 4),
                    "current_cagr_pct": None if current_cagr is None else round(current_cagr, 4),
                }
            )

        if invested_count < min_stocks:
            rows.append(
                BacktestRow(
                    start_date=as_of,
                    exit_date=holding_end_date,
                    history_eligible_count=len(history_eligible),
                    insufficient_history_count=insufficient_history_count,
                    passed_count=len(passed),
                    invested_count=invested_count,
                    initial_value=investment_per_stock * invested_count,
                    final_value=current_value,
                    portfolio_return_pct=None,
                    nifty_return_pct=None,
                    alpha_pct=None,
                    current_valued_count=current_valued_count,
                    current_value=current_value,
                    current_return_pct=None,
                    current_cagr_pct=None,
                    current_nifty_value=None,
                    current_nifty_return_pct=None,
                    current_nifty_cagr_pct=None,
                    current_alpha_pct=None,
                    stocks=passed,
                    skipped_reason=(
                        f"{len(passed)} passed full filter, but only {invested_count} had usable entry prices; "
                        f"minimum is {min_stocks}."
                    ),
                )
            )
            portfolio_records.append(
                build_portfolio_record(
                    row=rows[-1],
                    stock_records=stock_records,
                    investment_per_stock=investment_per_stock,
                    valuation_date=valuation_date,
                )
            )
            print_filter_month_result(rows[-1], valuation_date)
            continue

        initial_value = investment_per_stock * invested_count
        current_initial_value = investment_per_stock * current_valued_count
        current_portfolio_return_pct = (
            None if current_valued_count < min_stocks else ((current_value / current_initial_value) - 1) * 100
        )
        current_cagr = cagr_pct(as_of, valuation_date, current_portfolio_return_pct)
        current_nifty_return = None
        current_nifty_value = None
        current_nifty_cagr = None
        current_alpha = None
        if current_portfolio_return_pct is not None:
            current_nifty_return = nifty_return_until_pct(nifty_prices, as_of, valuation_date)
            current_nifty_value = (
                None if current_nifty_return is None else current_initial_value * (1 + current_nifty_return / 100)
            )
            current_nifty_cagr = cagr_pct(as_of, valuation_date, current_nifty_return)
            current_alpha = None if current_nifty_return is None else current_portfolio_return_pct - current_nifty_return

        rows.append(
            BacktestRow(
                start_date=as_of,
                exit_date=holding_end_date,
                history_eligible_count=len(history_eligible),
                insufficient_history_count=insufficient_history_count,
                passed_count=len(passed),
                invested_count=invested_count,
                initial_value=initial_value,
                final_value=current_value,
                portfolio_return_pct=None,
                nifty_return_pct=None,
                alpha_pct=None,
                current_valued_count=current_valued_count,
                current_value=current_value,
                current_return_pct=current_portfolio_return_pct,
                current_cagr_pct=current_cagr,
                current_nifty_value=current_nifty_value,
                current_nifty_return_pct=current_nifty_return,
                current_nifty_cagr_pct=current_nifty_cagr,
                current_alpha_pct=current_alpha,
                stocks=passed,
            )
        )
        portfolio_records.append(
            build_portfolio_record(
                row=rows[-1],
                stock_records=stock_records,
                investment_per_stock=investment_per_stock,
                valuation_date=valuation_date,
            )
        )
        print_filter_month_result(rows[-1], valuation_date)

    return rows, portfolio_records


def build_combined_return_rows(
    rows: list[BacktestRow],
    investment_per_stock: float,
    valuation_date: date,
) -> pd.DataFrame:
    summary = build_combined_summary(rows, investment_per_stock, valuation_date, "All portfolios combined")
    return pd.DataFrame([] if summary is None else [summary])


def write_outputs(
    rows: list[BacktestRow],
    portfolio_records: list[dict[str, object]],
    output_csv: Path,
    trades_csv: Path,
    investment_per_stock: float,
    valuation_date: date,
) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    trades_csv.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(portfolio_records)
    frame.to_csv(output_csv, index=False)
    frame.to_csv(trades_csv, index=False)


def main() -> int:
    args = parse_args()

    try:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end_date = (
            datetime.strptime(args.end_date, "%Y-%m-%d").date()
            if args.end_date
            else date.today()
        )
        valuation_date = (
            datetime.strptime(args.valuation_date, "%Y-%m-%d").date()
            if args.valuation_date
            else date.today()
        )
        post_july_2025_start = datetime.strptime(args.post_july_2025_start, "%Y-%m-%d").date()
    except ValueError as exc:
        print(f"Invalid date. Use YYYY-MM-DD. {exc}", file=sys.stderr)
        return 2

    if end_date < start_date:
        print("--end-date must be on or after --start-date", file=sys.stderr)
        return 2
    if args.required_history_months <= 0 or args.min_history_trading_days <= 0:
        print("History requirements must be positive.", file=sys.stderr)
        return 2

    if not args.symbols_file.exists():
        print(
            f"Symbols file not found: {args.symbols_file}. "
            "Pass --symbols-file with an existing CSV such as config/nifty_3300.csv. "
            "In Colab, use the upload widget version to upload nifty_3300.csv first.",
            file=sys.stderr,
        )
        return 2

    symbols = load_symbols(args.symbols_file, args.include_non_eq, args.max_symbols)
    formation_dates = yearly_formation_dates(start_date, end_date)
    if not formation_dates:
        print(
            "No yearly formation dates found in range. Use a range containing 15-May.",
            file=sys.stderr,
        )
        return 2
    all_download_symbols = list(dict.fromkeys(symbols + [NIFTY_50_SYMBOL]))

    history_download_start = (
        pd.Timestamp(min(formation_dates)) - pd.DateOffset(months=args.required_history_months)
    ).date() - timedelta(days=10)
    download_start = min(history_download_start, min(formation_dates) - timedelta(days=820))
    download_end = valuation_date + timedelta(days=2)

    print(f"Loaded {len(symbols)} stock symbols from {args.symbols_file}")
    print(
        f"Backtesting {len(formation_dates)} yearly portfolios from "
        f"{format_date(formation_dates[0])} to {format_date(formation_dates[-1])} "
        "(formation date: 15-May)"
    )
    print(f"Current valuation date: {format_date(valuation_date)}")
    print(f"Post-July-2025 summary starts from: {format_date(post_july_2025_start)}")
    print(
        "Point-in-time fundamentals: "
        f"annual lag {args.annual_report_lag_days}d, "
        f"shareholding lag {args.shareholding_lag_days}d"
    )
    print(f"Historical download range: {download_start.isoformat()} to {download_end.isoformat()}")

    finance_by_symbol: dict[str, dict[str, object] | None] = {}
    for index, symbol in enumerate(symbols, start=1):
        finance_by_symbol[symbol] = load_screener_finance(
            symbol,
            args.data_dir,
            fetch_missing=args.fetch_missing_screener,
        )
        if args.fetch_missing_screener and index % 25 == 0:
            print(f"Loaded/fetched Screener finance for {index}/{len(symbols)} symbols...", flush=True)
    print(f"Loaded Screener finance for {sum(1 for value in finance_by_symbol.values() if value is not None)} symbols")

    try:
        market_data = download_market_data(
            all_download_symbols,
            download_start,
            download_end,
            batch_size=args.batch_size,
            sleep_seconds=args.sleep_seconds,
            timeout_seconds=args.download_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Could not download market data: {exc}", file=sys.stderr)
        return 1

    rows, portfolio_records = run_backtest(
        symbols=symbols,
        market_data=market_data,
        finance_by_symbol=finance_by_symbol,
        formation_dates=formation_dates,
        investment_per_stock=args.investment_per_stock,
        min_stocks=args.min_stocks,
        valuation_date=valuation_date,
        required_history_months=args.required_history_months,
        min_history_trading_days=args.min_history_trading_days,
        annual_lag_days=args.annual_report_lag_days,
        shareholding_lag_days=args.shareholding_lag_days,
        assume_zero_pledge_when_missing=args.assume_zero_pledge_when_missing,
    )

    write_outputs(
        rows,
        portfolio_records,
        args.output_csv,
        args.trades_csv,
        args.investment_per_stock,
        valuation_date,
    )

    yearly_summary = build_yearly_summary(rows, args.investment_per_stock, valuation_date)
    args.yearly_summary_csv.parent.mkdir(parents=True, exist_ok=True)
    yearly_summary.to_csv(args.yearly_summary_csv, index=False)

    post_july_rows = [row for row in rows if row.start_date >= post_july_2025_start]
    post_july_summary = build_combined_summary(
        post_july_rows,
        args.investment_per_stock,
        valuation_date,
        label=f"Portfolios from {post_july_2025_start.isoformat()} onward",
    )
    args.post_july_2025_summary_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([] if post_july_summary is None else [post_july_summary]).to_csv(
        args.post_july_2025_summary_csv,
        index=False,
    )

    print_current_summary(rows, args.investment_per_stock, valuation_date)
    print(f"\nSummary CSV: {args.output_csv}")
    print(f"Portfolio CSV copy: {args.trades_csv}")
    print(f"Yearly summary CSV: {args.yearly_summary_csv}")
    print(f"Post-July-2025 summary CSV: {args.post_july_2025_summary_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
