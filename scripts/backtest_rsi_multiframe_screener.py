#!/usr/bin/env python3
"""
Backtest the RSI multi-timeframe swing screener.

For every month-start date, this script:
1. Builds the screener using price data available before that date.
2. Keeps only stocks with real price history at least 14 months before that date.
3. Selects stocks where weekly RSI > 60, monthly RSI > 60, and daily RSI < 40.
4. Invests an equal amount in every selected stock if at least 3 stocks pass.
5. Measures the portfolio return until the same calendar date next year.
6. Compares that return with NIFTY 50 over the same period.
7. Also values every invested portfolio at today's/latest available price.
8. Compares staggered portfolio cashflows with the same staggered NIFTY 50 cashflows.

Default run:
    python3 scripts/backtest_rsi_multiframe_screener.py

Useful faster test:
    python3 scripts/backtest_rsi_multiframe_screener.py --max-symbols 100 --end-date 2023-06-01
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - friendly runtime message
    yf = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SYMBOLS_FILE = PROJECT_ROOT / "config" / "Ticker_List_NSE_India_2000.csv"
DEFAULT_OUTPUT_CSV = PROJECT_ROOT / "data" / "rsi_multiframe_backtest_results.csv"
DEFAULT_TRADES_CSV = PROJECT_ROOT / "data" / "rsi_multiframe_backtest_trades.csv"
NIFTY_50_SYMBOL = "^NSEI"
DEFAULT_REQUIRED_HISTORY_MONTHS = 14
DEFAULT_MIN_HISTORY_TRADING_DAYS = 200
try:
    MONTH_END_FREQUENCY = "ME"
    pd.tseries.frequencies.to_offset(MONTH_END_FREQUENCY)
except ValueError:
    MONTH_END_FREQUENCY = "M"


@dataclass
class BacktestRow:
    start_date: date
    exit_date: date
    history_eligible_count: int
    insufficient_history_count: int
    passed_count: int
    invested_count: int
    initial_value: float
    final_value: float
    portfolio_return_pct: float | None
    nifty_return_pct: float | None
    alpha_pct: float | None
    current_valued_count: int
    current_value: float
    current_return_pct: float | None
    current_cagr_pct: float | None
    current_nifty_value: float | None
    current_nifty_return_pct: float | None
    current_nifty_cagr_pct: float | None
    current_alpha_pct: float | None
    stocks: list[str]
    skipped_reason: str = ""


def calculate_rsi(close: pd.Series, window: int = 14, adjust: bool = False) -> pd.Series:
    """Same RSI formula as the user's screener, adapted for a close-price Series."""
    close = close.dropna()
    delta = close.diff(1).dropna()
    loss = delta.copy()
    gains = delta.copy()

    gains[gains < 0] = 0
    loss[loss > 0] = 0

    gain_ewm = gains.ewm(com=window - 1, adjust=adjust).mean()
    loss_ewm = abs(loss.ewm(com=window - 1, adjust=adjust).mean())

    rs = gain_ewm / loss_ewm
    return 100 - 100 / (1 + rs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest stocks passing weekly/monthly high RSI and daily low RSI."
    )
    parser.add_argument("--symbols-file", type=Path, default=DEFAULT_SYMBOLS_FILE)
    parser.add_argument("--start-date", default="2023-03-01")
    parser.add_argument(
        "--end-date",
        default=None,
        help=(
            "Last portfolio start date. Default is the current month-start. "
            "Open portfolios will have N/A one-year return but will be included in current value."
        ),
    )
    parser.add_argument(
        "--valuation-date",
        default=None,
        help="Date for current/live portfolio valuation. Default is today.",
    )
    parser.add_argument("--investment-per-stock", type=float, default=100.0)
    parser.add_argument("--min-stocks", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=80)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument(
        "--required-history-months",
        type=int,
        default=DEFAULT_REQUIRED_HISTORY_MONTHS,
        help=(
            "Require each selected stock to have price data at least this many months before "
            "the portfolio date. Default is 14."
        ),
    )
    parser.add_argument(
        "--min-history-trading-days",
        type=int,
        default=DEFAULT_MIN_HISTORY_TRADING_DAYS,
        help=(
            "Minimum trading days required inside the required-history window. "
            "Default is 200."
        ),
    )
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--trades-csv", type=Path, default=DEFAULT_TRADES_CSV)
    parser.add_argument(
        "--include-non-eq",
        action="store_true",
        help="Include non-EQ series from the ticker list. Default keeps only SERIES == EQ.",
    )
    return parser.parse_args()


def load_symbols(symbols_file: Path, include_non_eq: bool, max_symbols: int | None) -> list[str]:
    if not symbols_file.exists():
        raise FileNotFoundError(f"Symbols file not found: {symbols_file}")

    frame = pd.read_csv(symbols_file)
    frame.columns = frame.columns.str.strip().str.upper()

    if "SERIES" in frame.columns and not include_non_eq:
        frame = frame[frame["SERIES"].astype(str).str.strip().str.upper() == "EQ"]

    yahoo_column = next(
        (column for column in ("YAHOO_EQUIVALENT_CODE", "YAHOOEQUIV", "YAHOO EQUIV") if column in frame.columns),
        None,
    )
    if yahoo_column:
        symbols = frame[yahoo_column].dropna().astype(str).str.strip()
    elif "SYMBOL" in frame.columns:
        symbols = frame["SYMBOL"].dropna().astype(str).str.strip()
    else:
        raise ValueError("Symbols CSV must have SYMBOL or Yahoo equivalent column.")

    cleaned: list[str] = []
    for symbol in symbols:
        symbol = symbol.strip().strip("'").strip('"').strip()
        if not symbol:
            continue
        if "," in symbol:
            symbol = symbol.split(",", 1)[0].strip().strip("'").strip('"')
        if not symbol.endswith(".NS") and not symbol.startswith("^"):
            symbol = f"{symbol}.NS"
        cleaned.append(symbol)

    unique_symbols = list(dict.fromkeys(cleaned))
    return unique_symbols[:max_symbols] if max_symbols else unique_symbols


def month_start_dates(start: date, end: date) -> list[date]:
    current = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    dates = []
    while current <= last:
        dates.append(current)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return dates


def add_one_year(value: date) -> date:
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        return value.replace(year=value.year + 1, day=28)


def latest_month_start(today: date) -> date:
    return date(today.year, today.month, 1)


def batched(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def download_close_data(
    symbols: list[str],
    start: date,
    end: date,
    batch_size: int,
    sleep_seconds: float,
) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
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
            threads=True,
        )

        for symbol in batch:
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    symbol_frame = data[symbol].copy()
                else:
                    symbol_frame = data.copy()
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

    return close_by_symbol, adjusted_by_symbol


def normalize_price_series(series: pd.Series) -> pd.Series:
    series = series.dropna().copy()
    series.index = pd.to_datetime(series.index).tz_localize(None)
    series = series[~series.index.duplicated(keep="last")]
    return series.sort_index()


def has_required_prior_history(
    close: pd.Series,
    as_of: date,
    required_history_months: int,
    min_history_trading_days: int,
) -> bool:
    if close is None or close.empty:
        return False

    as_of_ts = pd.Timestamp(as_of)
    cutoff = as_of_ts - pd.DateOffset(months=required_history_months)
    history_before_signal = close[close.index < as_of_ts].dropna()

    if history_before_signal.empty:
        return False

    first_available_date = history_before_signal.index[0]
    if first_available_date > cutoff:
        return False

    required_window = history_before_signal[history_before_signal.index >= cutoff]
    return len(required_window) >= min_history_trading_days


def rsi_at_signal_date(close: pd.Series, as_of: date, lookback_days: int, frequency: str | None) -> float | None:
    end = pd.Timestamp(as_of)
    start = end - pd.Timedelta(days=lookback_days)
    window = close[(close.index >= start) & (close.index < end)]
    if frequency:
        window = window.resample(frequency).last().dropna()
    if len(window) < 15:
        return None
    rsi = calculate_rsi(window)
    if rsi.empty:
        return None
    value = float(rsi.iloc[-1])
    return value if math.isfinite(value) else None


def passes_screener(close: pd.Series, as_of: date) -> bool:
    daily_rsi = rsi_at_signal_date(close, as_of, lookback_days=55, frequency=None)
    weekly_rsi = rsi_at_signal_date(close, as_of, lookback_days=214, frequency="W-FRI")
    monthly_rsi = rsi_at_signal_date(close, as_of, lookback_days=731, frequency=MONTH_END_FREQUENCY)
    if daily_rsi is None or weekly_rsi is None or monthly_rsi is None:
        return False
    return weekly_rsi > 60 and monthly_rsi > 60 and daily_rsi < 40


def first_price_on_or_after(series: pd.Series, target: date, max_days: int = 14) -> tuple[pd.Timestamp, float] | None:
    start = pd.Timestamp(target)
    end = start + pd.Timedelta(days=max_days)
    window = series[(series.index >= start) & (series.index <= end)].dropna()
    if window.empty:
        return None
    return window.index[0], float(window.iloc[0])


def last_price_on_or_before(series: pd.Series, target: date, max_days: int = 30) -> tuple[pd.Timestamp, float] | None:
    end = pd.Timestamp(target)
    start = end - pd.Timedelta(days=max_days)
    window = series[(series.index >= start) & (series.index <= end)].dropna()
    if window.empty:
        return None
    return window.index[-1], float(window.iloc[-1])


def stock_return(
    adjusted_close: pd.Series,
    start_date: date,
    exit_date: date,
) -> tuple[pd.Timestamp, float, pd.Timestamp, float, float] | None:
    entry = first_price_on_or_after(adjusted_close, start_date)
    exit_ = first_price_on_or_after(adjusted_close, exit_date)
    if entry is None or exit_ is None:
        return None
    entry_date, entry_price = entry
    exit_trade_date, exit_price = exit_
    if entry_price <= 0:
        return None
    return entry_date, entry_price, exit_trade_date, exit_price, (exit_price / entry_price) - 1


def run_backtest(
    symbols: list[str],
    close_by_symbol: dict[str, pd.Series],
    adjusted_by_symbol: dict[str, pd.Series],
    month_starts: list[date],
    investment_per_stock: float,
    min_stocks: int,
    valuation_date: date,
    required_history_months: int,
    min_history_trading_days: int,
) -> tuple[list[BacktestRow], list[dict[str, object]]]:
    rows: list[BacktestRow] = []
    trades: list[dict[str, object]] = []
    nifty_prices = adjusted_by_symbol.get(NIFTY_50_SYMBOL)

    for as_of in month_starts:
        exit_date = add_one_year(as_of)
        history_eligible = [
            symbol
            for symbol in symbols
            if symbol in close_by_symbol
            and has_required_prior_history(
                close_by_symbol[symbol],
                as_of,
                required_history_months=required_history_months,
                min_history_trading_days=min_history_trading_days,
            )
        ]
        insufficient_history_count = len(symbols) - len(history_eligible)
        passed = [symbol for symbol in history_eligible if passes_screener(close_by_symbol[symbol], as_of)]

        if len(passed) < min_stocks:
            rows.append(
                BacktestRow(
                    start_date=as_of,
                    exit_date=exit_date,
                    history_eligible_count=len(history_eligible),
                    insufficient_history_count=insufficient_history_count,
                    passed_count=len(passed),
                    invested_count=0,
                    initial_value=0.0,
                    final_value=0.0,
                    portfolio_return_pct=None,
                    nifty_return_pct=nifty_return_pct(nifty_prices, as_of, exit_date),
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
                        f"Only {len(passed)} stocks passed after {required_history_months}-month "
                        f"history filter; minimum is {min_stocks}."
                    ),
                )
            )
            print_month_result(rows[-1])
            continue

        one_year_final_value = 0.0
        one_year_exit_count = 0
        current_value = 0.0
        current_valued_count = 0
        invested_count = 0
        for symbol in passed:
            adjusted_close = adjusted_by_symbol.get(symbol, pd.Series(dtype=float))
            entry = first_price_on_or_after(adjusted_close, as_of)
            if entry is None:
                continue
            entry_date, entry_price = entry
            if entry_price <= 0:
                continue

            invested_count += 1
            quantity = investment_per_stock / entry_price

            exit_trade_date = None
            exit_price = None
            one_year_return_pct = None
            if exit_date <= valuation_date:
                exit_ = first_price_on_or_after(adjusted_close, exit_date)
                if exit_ is not None:
                    exit_trade_date, exit_price = exit_
                    one_year_exit_count += 1
                    one_year_value = quantity * exit_price
                    one_year_final_value += one_year_value
                    one_year_return_pct = ((exit_price / entry_price) - 1) * 100

            current_trade_date = None
            current_price = None
            current_trade_value = None
            current_return_pct = None
            current = last_price_on_or_before(adjusted_close, valuation_date)
            if current is not None:
                current_trade_date, current_price = current
                current_trade_value = quantity * current_price
                current_value += current_trade_value
                current_valued_count += 1
                current_return_pct = ((current_price / entry_price) - 1) * 100

            trades.append(
                {
                    "portfolio_start_date": as_of.isoformat(),
                    "portfolio_exit_date": exit_date.isoformat(),
                    "symbol": symbol,
                    "entry_trade_date": entry_date.date().isoformat(),
                    "entry_price": round(entry_price, 4),
                    "quantity": round(quantity, 8),
                    "one_year_exit_trade_date": exit_trade_date.date().isoformat() if exit_trade_date is not None else "",
                    "one_year_exit_price": None if exit_price is None else round(exit_price, 4),
                    "one_year_return_pct": None if one_year_return_pct is None else round(one_year_return_pct, 4),
                    "current_valuation_date": (
                        current_trade_date.date().isoformat() if current_trade_date is not None else ""
                    ),
                    "current_price": None if current_price is None else round(current_price, 4),
                    "current_value": None if current_trade_value is None else round(current_trade_value, 2),
                    "current_return_pct": None if current_return_pct is None else round(current_return_pct, 4),
                }
            )

        if invested_count < min_stocks:
            rows.append(
                BacktestRow(
                    start_date=as_of,
                    exit_date=exit_date,
                    history_eligible_count=len(history_eligible),
                    insufficient_history_count=insufficient_history_count,
                    passed_count=len(passed),
                    invested_count=invested_count,
                    initial_value=investment_per_stock * invested_count,
                    final_value=one_year_final_value,
                    portfolio_return_pct=None,
                    nifty_return_pct=nifty_return_pct(nifty_prices, as_of, exit_date),
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
                        f"{len(passed)} passed, but only {invested_count} had usable entry prices; "
                        f"minimum is {min_stocks}."
                    ),
                )
            )
            print_month_result(rows[-1])
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
            current_alpha = (
                None if current_nifty_return is None else current_portfolio_return_pct - current_nifty_return
            )

        portfolio_return_pct = None
        nifty_return = None
        alpha = None
        if one_year_exit_count >= min_stocks:
            one_year_initial_value = investment_per_stock * one_year_exit_count
            portfolio_return_pct = ((one_year_final_value / one_year_initial_value) - 1) * 100
            nifty_return = nifty_return_pct(nifty_prices, as_of, exit_date)
            alpha = None if nifty_return is None else portfolio_return_pct - nifty_return

        rows.append(
            BacktestRow(
                start_date=as_of,
                exit_date=exit_date,
                history_eligible_count=len(history_eligible),
                insufficient_history_count=insufficient_history_count,
                passed_count=len(passed),
                invested_count=invested_count,
                initial_value=initial_value,
                final_value=one_year_final_value,
                portfolio_return_pct=portfolio_return_pct,
                nifty_return_pct=nifty_return,
                alpha_pct=alpha,
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
        print_month_result(rows[-1])

    return rows, trades


def nifty_return_pct(nifty_prices: pd.Series | None, start_date: date, exit_date: date) -> float | None:
    if nifty_prices is None or nifty_prices.empty:
        return None
    result = stock_return(nifty_prices, start_date, exit_date)
    if result is None:
        return None
    return result[-1] * 100


def nifty_return_until_pct(nifty_prices: pd.Series | None, start_date: date, valuation_date: date) -> float | None:
    if nifty_prices is None or nifty_prices.empty:
        return None
    entry = first_price_on_or_after(nifty_prices, start_date)
    current = last_price_on_or_before(nifty_prices, valuation_date)
    if entry is None or current is None:
        return None
    _, entry_price = entry
    _, current_price = current
    if entry_price <= 0:
        return None
    return ((current_price / entry_price) - 1) * 100


def cagr_pct(start_date: date, end_date: date, total_return_pct: float | None) -> float | None:
    if total_return_pct is None or end_date <= start_date:
        return None
    years = (end_date - start_date).days / 365.25
    if years <= 0:
        return None
    ending_multiple = 1 + total_return_pct / 100
    if ending_multiple <= 0:
        return None
    return (ending_multiple ** (1 / years) - 1) * 100


def xnpv(rate: float, cashflows: list[tuple[date, float]]) -> float:
    first_date = cashflows[0][0]
    return sum(amount / ((1 + rate) ** ((cashflow_date - first_date).days / 365.25)) for cashflow_date, amount in cashflows)


def xirr_pct(cashflows: list[tuple[date, float]]) -> float | None:
    if not cashflows:
        return None
    has_positive = any(amount > 0 for _, amount in cashflows)
    has_negative = any(amount < 0 for _, amount in cashflows)
    if not has_positive or not has_negative:
        return None

    cashflows = sorted(cashflows, key=lambda item: item[0])
    low = -0.9999
    high = 1.0
    low_value = xnpv(low, cashflows)
    high_value = xnpv(high, cashflows)

    while low_value * high_value > 0 and high < 1000:
        high *= 2
        high_value = xnpv(high, cashflows)

    if low_value * high_value > 0:
        return None

    for _ in range(120):
        mid = (low + high) / 2
        mid_value = xnpv(mid, cashflows)
        if abs(mid_value) < 1e-7:
            return mid * 100
        if low_value * mid_value <= 0:
            high = mid
            high_value = mid_value
        else:
            low = mid
            low_value = mid_value

    return ((low + high) / 2) * 100


def format_date(value: date) -> str:
    return value.strftime("%d-%b-%Y").lstrip("0")


def fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}%"


def fmt_money(value: float | None) -> str:
    return "N/A" if value is None else f"Rs {value:.2f}"


def print_month_result(row: BacktestRow) -> None:
    heading = (
        f"{format_date(row.start_date)} "
        f"(eligible {row.history_eligible_count}, excluded no 14m data "
        f"{row.insufficient_history_count}, RSI passed {row.passed_count})"
    )
    if row.skipped_reason:
        print(f"{heading}: skipped - {row.skipped_reason}")
        return
    one_year_text = (
        f"return till {format_date(row.exit_date)} if invested Rs 100 in each is total "
        f"{fmt_pct(row.portfolio_return_pct)} return; NIFTY 50 gave {fmt_pct(row.nifty_return_pct)}; "
        f"alpha {fmt_pct(row.alpha_pct)}"
    )
    current_text = (
        f"from portfolio date till now: current value Rs {row.current_value:.2f} on invested Rs "
        f"{row.current_valued_count * 100:.2f}; portfolio P/L {fmt_pct(row.current_return_pct)}; "
        f"portfolio CAGR {fmt_pct(row.current_cagr_pct)}; NIFTY value "
        f"{fmt_money(row.current_nifty_value)}; NIFTY 50 {fmt_pct(row.current_nifty_return_pct)}; "
        f"NIFTY CAGR {fmt_pct(row.current_nifty_cagr_pct)}; alpha {fmt_pct(row.current_alpha_pct)}"
    )
    print(f"{heading}: {one_year_text}; {current_text}.")


def print_current_summary(rows: list[BacktestRow], investment_per_stock: float, valuation_date: date) -> None:
    valued_rows = [
        row
        for row in rows
        if row.current_return_pct is not None and row.current_valued_count > 0 and row.current_nifty_value is not None
    ]
    if not valued_rows:
        print("\nCurrent portfolio summary: no current portfolio rows available.")
        return

    total_invested = sum(investment_per_stock * row.current_valued_count for row in valued_rows)
    total_current_value = sum(row.current_value for row in valued_rows)
    total_nifty_value = sum(float(row.current_nifty_value) for row in valued_rows)
    total_profit_loss = total_current_value - total_invested
    total_nifty_profit_loss = total_nifty_value - total_invested
    total_return_pct = (total_current_value / total_invested - 1) * 100
    total_nifty_return_pct = (total_nifty_value / total_invested - 1) * 100
    alpha_pct = total_return_pct - total_nifty_return_pct

    portfolio_cashflows = [(row.start_date, -(investment_per_stock * row.current_valued_count)) for row in valued_rows]
    portfolio_cashflows.append((valuation_date, total_current_value))

    nifty_cashflows = [(row.start_date, -(investment_per_stock * row.current_valued_count)) for row in valued_rows]
    nifty_cashflows.append((valuation_date, total_nifty_value))

    portfolio_xirr = xirr_pct(portfolio_cashflows)
    nifty_xirr = xirr_pct(nifty_cashflows)

    print("\nCurrent combined portfolio vs same cashflows in NIFTY 50")
    print(f"Valuation date: {format_date(valuation_date)}")
    print(f"Portfolio rows valued: {len(valued_rows)}")
    print(f"Total stock buys valued today: {sum(row.current_valued_count for row in valued_rows)}")
    print(f"Total invested: Rs {total_invested:.2f}")
    print(f"Portfolio current value: Rs {total_current_value:.2f}")
    print(f"Portfolio profit/loss: Rs {total_profit_loss:.2f}")
    print(f"Portfolio total return: {total_return_pct:.2f}%")
    print(f"Portfolio XIRR: {fmt_pct(portfolio_xirr)}")
    print(f"NIFTY 50 current value for same cashflows: Rs {total_nifty_value:.2f}")
    print(f"NIFTY 50 profit/loss for same cashflows: Rs {total_nifty_profit_loss:.2f}")
    print(f"NIFTY 50 total return for same cashflows: {total_nifty_return_pct:.2f}%")
    print(f"NIFTY 50 XIRR for same cashflows: {fmt_pct(nifty_xirr)}")
    print(f"Total alpha vs NIFTY 50: {alpha_pct:.2f}%")


def write_outputs(rows: list[BacktestRow], trades: list[dict[str, object]], output_csv: Path, trades_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    trades_csv.parent.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(
        [
            {
                "start_date": row.start_date.isoformat(),
                "exit_date": row.exit_date.isoformat(),
                "history_eligible_count": row.history_eligible_count,
                "insufficient_14m_history_count": row.insufficient_history_count,
                "passed_count": row.passed_count,
                "invested_count": row.invested_count,
                "initial_value": round(row.initial_value, 2),
                "final_value": round(row.final_value, 2),
                "portfolio_return_pct": None if row.portfolio_return_pct is None else round(row.portfolio_return_pct, 4),
                "nifty50_return_pct": None if row.nifty_return_pct is None else round(row.nifty_return_pct, 4),
                "alpha_pct": None if row.alpha_pct is None else round(row.alpha_pct, 4),
                "current_valued_count": row.current_valued_count,
                "current_value": round(row.current_value, 2),
                "current_return_pct": None if row.current_return_pct is None else round(row.current_return_pct, 4),
                "current_cagr_pct": None if row.current_cagr_pct is None else round(row.current_cagr_pct, 4),
                "current_nifty50_value": None if row.current_nifty_value is None else round(row.current_nifty_value, 2),
                "current_nifty50_return_pct": (
                    None if row.current_nifty_return_pct is None else round(row.current_nifty_return_pct, 4)
                ),
                "current_nifty50_cagr_pct": (
                    None if row.current_nifty_cagr_pct is None else round(row.current_nifty_cagr_pct, 4)
                ),
                "current_alpha_pct": None if row.current_alpha_pct is None else round(row.current_alpha_pct, 4),
                "stocks": ",".join(row.stocks),
                "skipped_reason": row.skipped_reason,
            }
            for row in rows
        ]
    ).to_csv(output_csv, index=False)

    pd.DataFrame(trades).to_csv(trades_csv, index=False)


def main() -> int:
    args = parse_args()

    try:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end_date = (
            datetime.strptime(args.end_date, "%Y-%m-%d").date()
            if args.end_date
            else latest_month_start(date.today())
        )
        valuation_date = (
            datetime.strptime(args.valuation_date, "%Y-%m-%d").date()
            if args.valuation_date
            else date.today()
        )
    except ValueError as exc:
        print(f"Invalid date. Use YYYY-MM-DD. {exc}", file=sys.stderr)
        return 2

    if end_date < start_date:
        print("--end-date must be on or after --start-date", file=sys.stderr)
        return 2
    if args.required_history_months <= 0:
        print("--required-history-months must be positive", file=sys.stderr)
        return 2
    if args.min_history_trading_days <= 0:
        print("--min-history-trading-days must be positive", file=sys.stderr)
        return 2

    symbols = load_symbols(args.symbols_file, args.include_non_eq, args.max_symbols)
    month_starts = month_start_dates(start_date, end_date)
    all_download_symbols = list(dict.fromkeys(symbols + [NIFTY_50_SYMBOL]))

    history_download_start = (
        pd.Timestamp(min(month_starts)) - pd.DateOffset(months=args.required_history_months)
    ).date() - timedelta(days=10)
    download_start = min(history_download_start, min(month_starts) - timedelta(days=760))
    download_end = max(add_one_year(max(month_starts)) + timedelta(days=21), valuation_date + timedelta(days=2))

    print(f"Loaded {len(symbols)} stock symbols from {args.symbols_file}")
    print(f"Backtesting {len(month_starts)} monthly portfolios from {format_date(month_starts[0])} to {format_date(month_starts[-1])}")
    print(f"Current valuation date: {format_date(valuation_date)}")
    print(
        f"Required stock history: at least {args.required_history_months} months before each "
        f"portfolio date, with minimum {args.min_history_trading_days} trading days in that window"
    )
    print(f"Historical download range: {download_start.isoformat()} to {download_end.isoformat()}")

    try:
        close_by_symbol, adjusted_by_symbol = download_close_data(
            all_download_symbols,
            download_start,
            download_end,
            batch_size=args.batch_size,
            sleep_seconds=args.sleep_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Could not download market data: {exc}", file=sys.stderr)
        return 1

    rows, trades = run_backtest(
        symbols=symbols,
        close_by_symbol=close_by_symbol,
        adjusted_by_symbol=adjusted_by_symbol,
        month_starts=month_starts,
        investment_per_stock=args.investment_per_stock,
        min_stocks=args.min_stocks,
        valuation_date=valuation_date,
        required_history_months=args.required_history_months,
        min_history_trading_days=args.min_history_trading_days,
    )
    write_outputs(rows, trades, args.output_csv, args.trades_csv)
    print_current_summary(rows, args.investment_per_stock, valuation_date)

    print(f"\nSummary CSV: {args.output_csv}")
    print(f"Trade CSV: {args.trades_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
