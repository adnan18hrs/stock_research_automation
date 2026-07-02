#!/usr/bin/env python3
"""
NIFTY 50 option signal executor for Kite Connect v3.

Signal rules:
  buy  -> buy the nearest NIFTY CE strike above spot, rounded to 100.
  sell -> buy the nearest NIFTY PE strike below spot, rounded to 100.

Risk management:
  - At +5% option LTP, exit half the quantity.
  - Move remaining stop loss to entry price.
  - Trail the remaining stop by half of the option's move from entry.

The script is dry-run by default. Live orders require both:
  --execute --i-understand-live-risk

Environment variables:
  KITE_API_KEY
  KITE_ACCESS_TOKEN
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as datetime_time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    ZoneInfo = None  # type: ignore


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_STATE_FILE = ROOT_DIR / "logs" / "nifty50_option_algo_state.json"
DEFAULT_DAILY_PNL_FILE = ROOT_DIR / "logs" / "nifty50_option_algo_daily_pnl.json"
DEFAULT_INSTRUMENT_CACHE = ROOT_DIR / "logs" / "kite_nfo_instruments.csv"
KITE_API_BASE = "https://api.kite.trade"
NIFTY_SPOT_KEY = "NSE:NIFTY 50"
FIXED_LOTS = 2
LOG = logging.getLogger("nifty50_option_algo")


class KiteApiError(RuntimeError):
    pass


@dataclass
class OptionInstrument:
    instrument_token: int
    tradingsymbol: str
    exchange: str
    expiry: date
    strike: int
    lot_size: int
    instrument_type: str


@dataclass
class ManagedPosition:
    signal: str
    tradingsymbol: str
    exchange: str
    entry_price: float
    initial_qty: int
    remaining_qty: int
    half_exit_qty: int
    lot_size: int
    profit_trigger_pct: float
    initial_stop_loss_pct: float
    initial_stop_loss: float
    highest_ltp: float
    stop_loss: Optional[float]
    partial_exit_done: bool
    exit_reason: Optional[str]
    realized_pnl: float
    entry_order_id: str
    created_at: str


class KiteClient:
    def __init__(
        self,
        api_key: Optional[str],
        access_token: Optional[str],
        *,
        execute: bool,
        timeout: int = 15,
    ) -> None:
        self.api_key = api_key
        self.access_token = access_token
        self.execute = execute
        self.timeout = timeout

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.access_token)

    def _headers(self) -> Dict[str, str]:
        headers = {"X-Kite-Version": "3"}
        if self.has_credentials:
            headers["Authorization"] = f"token {self.api_key}:{self.access_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        raw: bool = False,
    ) -> Any:
        url = f"{KITE_API_BASE}{path}"
        data = None
        started_at = time.monotonic()

        if method == "GET" and params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{query}"
        elif params:
            data = urllib.parse.urlencode(params).encode("utf-8")

        LOG.debug("Kite request start method=%s path=%s raw=%s", method, path, raw)
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=self._headers(),
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            LOG.error("Kite request failed method=%s path=%s status=%s", method, path, exc.code)
            raise KiteApiError(f"Kite HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            LOG.error("Kite network error method=%s path=%s reason=%s", method, path, exc.reason)
            raise KiteApiError(f"Kite network error: {exc.reason}") from exc

        elapsed_ms = (time.monotonic() - started_at) * 1000
        LOG.debug(
            "Kite request complete method=%s path=%s elapsed_ms=%.0f",
            method,
            path,
            elapsed_ms,
        )

        if raw:
            return body.decode("utf-8")

        if "application/json" not in content_type:
            raise KiteApiError(f"Unexpected response content type: {content_type}")

        payload = json.loads(body.decode("utf-8"))
        if payload.get("status") != "success":
            raise KiteApiError(payload.get("message", "Kite API returned an error"))
        return payload.get("data")

    def quote_ltp(self, instruments: Iterable[str]) -> Dict[str, float]:
        instrument_list = list(instruments)
        LOG.debug("Fetching LTP for %s", ", ".join(instrument_list))
        data = self._request("GET", "/quote/ltp", params={"i": instrument_list})
        return {
            key: float(value["last_price"])
            for key, value in data.items()
            if value and value.get("last_price") is not None
        }

    def instruments_csv(self, exchange: str) -> str:
        LOG.info("Downloading fresh %s instruments from Kite", exchange)
        return self._request("GET", f"/instruments/{exchange}", raw=True)

    def place_market_order(
        self,
        *,
        variety: str,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        product: str,
        tag: str,
        market_protection: float,
    ) -> str:
        if not self.execute:
            LOG.info(
                "DRY RUN order: %s %s %s:%s product=%s market_protection=%s",
                transaction_type,
                quantity,
                exchange,
                tradingsymbol,
                product,
                market_protection,
            )
            return f"DRYRUN-{int(time.time())}"

        LOG.warning(
            "LIVE order submit: %s %s %s:%s product=%s variety=%s market_protection=%s",
            transaction_type,
            quantity,
            exchange,
            tradingsymbol,
            product,
            variety,
            market_protection,
        )
        data = self._request(
            "POST",
            f"/orders/{variety}",
            params={
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type,
                "quantity": quantity,
                "product": product,
                "order_type": "MARKET",
                "validity": "DAY",
                "tag": tag[:20],
                "market_protection": market_protection,
            },
        )
        order_id = data.get("order_id")
        if not order_id:
            raise KiteApiError(f"Order response did not include order_id: {data}")
        LOG.warning("LIVE order accepted: order_id=%s", order_id)
        return str(order_id)

    def order_history(self, order_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", f"/orders/{order_id}")


def ist_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    return datetime.now()


def default_log_file() -> Path:
    return ROOT_DIR / "logs" / f"nifty50_option_algo_{ist_now().strftime('%Y%m%d')}.log"


def configure_logging(args: argparse.Namespace) -> Path:
    log_file = args.log_file or default_log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()
    LOG.propagate = False

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, args.log_level))
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s IST | %(levelname)s | %(message)s", "%H:%M:%S")
    )

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=args.log_max_bytes,
        backupCount=args.log_backup_count,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s IST | %(levelname)s | %(name)s | %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )

    LOG.addHandler(console_handler)
    LOG.addHandler(file_handler)
    LOG.info("Logging to %s", log_file)
    return log_file


def parse_expiry(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_hhmm(value: str) -> datetime_time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use HH:MM format, for example 15:25") from exc


def round_price(value: float) -> float:
    return round(value, 2)


def load_daily_pnl(pnl_file: Path) -> Dict[str, float]:
    if not pnl_file.exists():
        return {}
    try:
        data = json.loads(pnl_file.read_text())
    except json.JSONDecodeError:
        LOG.warning("Daily PnL file is not valid JSON, ignoring: %s", pnl_file)
        return {}
    return {str(day): float(value) for day, value in data.items()}


def daily_pnl_for_today(pnl_file: Path) -> float:
    return load_daily_pnl(pnl_file).get(ist_now().date().isoformat(), 0.0)


def record_daily_pnl(pnl_file: Path, pnl: float) -> float:
    pnl_file.parent.mkdir(parents=True, exist_ok=True)
    data = load_daily_pnl(pnl_file)
    today = ist_now().date().isoformat()
    data[today] = round_price(data.get(today, 0.0) + pnl)
    pnl_file.write_text(json.dumps(data, indent=2, sort_keys=True))
    return data[today]


def ensure_daily_loss_allowed(args: argparse.Namespace) -> None:
    if args.max_daily_loss is None:
        return
    today_pnl = daily_pnl_for_today(args.daily_pnl_file)
    if today_pnl <= -abs(args.max_daily_loss):
        raise SystemExit(
            f"Daily loss guard active. Today's recorded PnL is {today_pnl:.2f}, "
            f"max allowed loss is {abs(args.max_daily_loss):.2f}. Algo stopped."
        )


def force_exit_due(force_exit_time: Optional[datetime_time]) -> bool:
    return bool(force_exit_time and ist_now().time() >= force_exit_time)


def ensure_live_confirmation(args: argparse.Namespace) -> bool:
    if not args.execute:
        return False
    if not args.i_understand_live_risk:
        raise SystemExit(
            "Live mode blocked. Add --i-understand-live-risk with --execute "
            "only when you are ready to place real Kite orders."
        )
    return True


def selected_strike(signal: str, nifty_ltp: float) -> int:
    lower = math.floor(nifty_ltp / 100.0) * 100
    upper = math.ceil(nifty_ltp / 100.0) * 100

    if signal == "buy":
        return int(upper + 100 if upper <= nifty_ltp else upper)
    if signal == "sell":
        return int(lower - 100 if lower >= nifty_ltp else lower)
    raise ValueError(f"Unsupported signal: {signal}")


def load_or_refresh_nfo_instruments(
    client: KiteClient,
    *,
    cache_file: Path,
    refresh: bool,
) -> str:
    if not refresh and cache_file.exists() and cache_file.stat().st_size > 0:
        modified_at = datetime.fromtimestamp(cache_file.stat().st_mtime).date()
        if modified_at == ist_now().date():
            LOG.info("Using today's NFO instrument cache: %s", cache_file)
            return cache_file.read_text()
        LOG.info("Instrument cache is stale: %s modified_at=%s", cache_file, modified_at)

    if not client.has_credentials:
        raise SystemExit(
            "Kite credentials are required to download NFO instruments. "
            "Set KITE_API_KEY and KITE_ACCESS_TOKEN, or keep an up-to-date "
            f"cache at {cache_file}."
        )

    text = client.instruments_csv("NFO")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text)
    LOG.info("Saved fresh NFO instrument cache: %s", cache_file)
    return text


def parse_nifty_options(instruments_csv_text: str) -> List[OptionInstrument]:
    rows = csv.DictReader(instruments_csv_text.splitlines())
    options: List[OptionInstrument] = []
    for row in rows:
        if row.get("exchange") != "NFO":
            continue
        if row.get("name") != "NIFTY":
            continue
        if row.get("instrument_type") not in {"CE", "PE"}:
            continue
        try:
            options.append(
                OptionInstrument(
                    instrument_token=int(row["instrument_token"]),
                    tradingsymbol=row["tradingsymbol"],
                    exchange=row["exchange"],
                    expiry=parse_expiry(row["expiry"]),
                    strike=int(float(row["strike"])),
                    lot_size=int(row["lot_size"]),
                    instrument_type=row["instrument_type"],
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    LOG.info("Parsed %s NIFTY option instruments from NFO cache", len(options))
    return options


def choose_option(
    options: List[OptionInstrument],
    *,
    signal: str,
    strike: int,
    as_of: date,
) -> OptionInstrument:
    instrument_type = "CE" if signal == "buy" else "PE"
    matches = [
        option
        for option in options
        if option.instrument_type == instrument_type
        and option.strike == strike
        and option.expiry >= as_of
    ]
    if not matches:
        raise SystemExit(
            f"No NIFTY {instrument_type} instrument found for strike {strike}. "
            "Refresh the NFO instruments cache and try again."
        )
    selected = sorted(matches, key=lambda item: (item.expiry, item.tradingsymbol))[0]
    LOG.info(
        "Selected option: %s:%s type=%s strike=%s expiry=%s lot_size=%s",
        selected.exchange,
        selected.tradingsymbol,
        selected.instrument_type,
        selected.strike,
        selected.expiry,
        selected.lot_size,
    )
    return selected


def get_nifty_ltp(client: KiteClient, paper_nifty_ltp: Optional[float]) -> float:
    if paper_nifty_ltp is not None:
        LOG.info("Using paper NIFTY LTP: %.2f", paper_nifty_ltp)
        return paper_nifty_ltp
    if not client.has_credentials:
        raise SystemExit(
            "Kite credentials are required for live NIFTY LTP. "
            "Set KITE_API_KEY and KITE_ACCESS_TOKEN, or pass --paper-nifty-ltp."
        )
    prices = client.quote_ltp([NIFTY_SPOT_KEY])
    try:
        ltp = prices[NIFTY_SPOT_KEY]
        LOG.info("NIFTY spot LTP: %.2f", ltp)
        return ltp
    except KeyError as exc:
        raise KiteApiError(f"NIFTY LTP missing from quote response: {prices}") from exc


def get_option_ltp(
    client: KiteClient,
    instrument: OptionInstrument,
    paper_option_ltp: Optional[float],
) -> float:
    if paper_option_ltp is not None:
        LOG.debug("Using paper option LTP for %s: %.2f", instrument.tradingsymbol, paper_option_ltp)
        return paper_option_ltp
    prices = client.quote_ltp([f"{instrument.exchange}:{instrument.tradingsymbol}"])
    key = f"{instrument.exchange}:{instrument.tradingsymbol}"
    try:
        ltp = prices[key]
        LOG.debug("Option LTP %s: %.2f", key, ltp)
        return ltp
    except KeyError as exc:
        raise KiteApiError(f"Option LTP missing from quote response: {prices}") from exc


def order_rejection_detail(order: Dict[str, Any]) -> str:
    useful_keys = [
        "status",
        "status_message",
        "status_message_raw",
        "rejected_by",
        "exchange",
        "tradingsymbol",
        "transaction_type",
        "order_type",
        "product",
        "quantity",
        "filled_quantity",
        "pending_quantity",
        "price",
        "average_price",
        "validity",
        "order_timestamp",
        "exchange_timestamp",
        "exchange_update_timestamp",
    ]
    parts = []
    for key in useful_keys:
        value = order.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return " | ".join(parts) if parts else json.dumps(order, sort_keys=True)


def wait_for_entry_price(
    client: KiteClient,
    *,
    order_id: str,
    fallback_ltp: float,
    timeout_seconds: int,
) -> float:
    if not client.execute:
        LOG.info("Dry-run entry price set from current option LTP: %.2f", fallback_ltp)
        return fallback_ltp

    deadline = time.time() + timeout_seconds
    last_status = None
    LOG.info("Waiting for entry order fill: order_id=%s timeout=%ss", order_id, timeout_seconds)
    while time.time() < deadline:
        history = client.order_history(order_id)
        if history:
            last = history[-1]
            last_status = last.get("status")
            average_price = float(last.get("average_price") or 0)
            status_message = last.get("status_message") or last.get("status_message_raw") or "-"
            LOG.info(
                "Entry order status: order_id=%s status=%s average_price=%.2f message=%s",
                order_id,
                last_status,
                average_price,
                status_message,
            )
            if last_status == "COMPLETE" and average_price > 0:
                return average_price
            if last_status in {"REJECTED", "CANCELLED"}:
                detail = order_rejection_detail(last)
                LOG.error("Entry order failed: order_id=%s | %s", order_id, detail)
                raise KiteApiError(f"Entry order {order_id} {last_status}: {detail}")
        time.sleep(1)
    raise KiteApiError(f"Entry order {order_id} not complete. Last status: {last_status}")


def save_position(position: ManagedPosition, state_file: Path) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(asdict(position), indent=2, sort_keys=True))
    LOG.debug("Saved position state to %s", state_file)


def load_position(state_file: Path) -> ManagedPosition:
    if not state_file.exists():
        raise SystemExit(f"No saved position found at {state_file}")
    data = json.loads(state_file.read_text())
    entry_price = float(data["entry_price"])
    profit_trigger_pct = float(data.get("profit_trigger_pct", 0.10))
    initial_stop_loss_pct = float(data.get("initial_stop_loss_pct", 0.10))
    data.setdefault("profit_trigger_pct", profit_trigger_pct)
    data.setdefault("initial_stop_loss_pct", initial_stop_loss_pct)
    data.setdefault("initial_stop_loss", round_price(entry_price * (1 - initial_stop_loss_pct)))
    data.setdefault("stop_loss", data["initial_stop_loss"])
    data.setdefault("exit_reason", None)
    data.setdefault("realized_pnl", 0.0)
    position = ManagedPosition(**data)
    LOG.info(
        "Loaded position state: %s:%s remaining_qty=%s partial_exit_done=%s stop_loss=%s exit_reason=%s",
        position.exchange,
        position.tradingsymbol,
        position.remaining_qty,
        position.partial_exit_done,
        position.stop_loss,
        position.exit_reason,
    )
    return position


def start_position(args: argparse.Namespace) -> ManagedPosition:
    execute = ensure_live_confirmation(args)
    ensure_daily_loss_allowed(args)
    client = KiteClient(
        os.environ.get("KITE_API_KEY"),
        os.environ.get("KITE_ACCESS_TOKEN"),
        execute=execute,
    )
    if args.lots != FIXED_LOTS:
        LOG.warning("Ignoring --lots=%s. This strategy always uses exactly %s lots.", args.lots, FIXED_LOTS)
    args.lots = FIXED_LOTS
    LOG.info("Starting new signal: signal=%s mode=%s lots=%s product=%s", args.signal, "LIVE" if execute else "DRY_RUN", args.lots, args.product)

    nifty_ltp = get_nifty_ltp(client, args.paper_nifty_ltp)
    strike = selected_strike(args.signal, nifty_ltp)
    instrument_type = "CE" if args.signal == "buy" else "PE"
    LOG.info(
        "Strike rule resolved: signal=%s nifty_ltp=%.2f option_type=%s strike=%s",
        args.signal,
        nifty_ltp,
        instrument_type,
        strike,
    )

    nfo_csv = load_or_refresh_nfo_instruments(
        client,
        cache_file=args.instrument_cache,
        refresh=args.refresh_instruments,
    )
    instrument = choose_option(
        parse_nifty_options(nfo_csv),
        signal=args.signal,
        strike=strike,
        as_of=ist_now().date(),
    )

    initial_qty = args.lots * instrument.lot_size
    half_exit_qty = instrument.lot_size
    if half_exit_qty <= 0 or half_exit_qty >= initial_qty:
        raise SystemExit("Could not calculate a valid half-exit quantity.")

    option_ltp = get_option_ltp(client, instrument, args.paper_option_ltp)
    LOG.info(
        "Entry plan: signal=%s NIFTY=%.2f option=%s:%s type=%s strike=%s expiry=%s lot_size=%s lots=%s qty=%s half_exit_qty=%s current_option_ltp=%.2f",
        args.signal,
        nifty_ltp,
        instrument.exchange,
        instrument.tradingsymbol,
        instrument_type,
        strike,
        instrument.expiry,
        instrument.lot_size,
        args.lots,
        initial_qty,
        half_exit_qty,
        option_ltp,
    )

    order_id = client.place_market_order(
        variety=args.variety,
        exchange=instrument.exchange,
        tradingsymbol=instrument.tradingsymbol,
        transaction_type="BUY",
        quantity=initial_qty,
        product=args.product,
        tag=args.tag,
        market_protection=args.market_protection,
    )
    entry_price = wait_for_entry_price(
        client,
        order_id=order_id,
        fallback_ltp=option_ltp,
        timeout_seconds=args.order_timeout,
    )

    position = ManagedPosition(
        signal=args.signal,
        tradingsymbol=instrument.tradingsymbol,
        exchange=instrument.exchange,
        entry_price=entry_price,
        initial_qty=initial_qty,
        remaining_qty=initial_qty,
        half_exit_qty=half_exit_qty,
        lot_size=instrument.lot_size,
        profit_trigger_pct=args.profit_trigger_pct,
        initial_stop_loss_pct=args.initial_stop_loss_pct,
        initial_stop_loss=round_price(entry_price * (1 - args.initial_stop_loss_pct)),
        highest_ltp=entry_price,
        stop_loss=round_price(entry_price * (1 - args.initial_stop_loss_pct)),
        partial_exit_done=False,
        exit_reason=None,
        realized_pnl=0.0,
        entry_order_id=order_id,
        created_at=ist_now().isoformat(timespec="seconds"),
    )
    save_position(position, args.state_file)
    LOG.info("Saved position state to %s", args.state_file)
    if execute:
        LOG.info(
            "LIVE position opened: entry_price=%.2f profit_trigger=%.2f initial_qty=%s remaining_qty=%s order_id=%s",
            entry_price,
            entry_price * (1 + args.profit_trigger_pct),
            position.initial_qty,
            position.remaining_qty,
            order_id,
        )
    else:
        LOG.info(
            "SIMULATED position opened only in this script: entry_price=%.2f profit_trigger=%.2f initial_qty=%s remaining_qty=%s dry_run_order_id=%s",
            entry_price,
            entry_price * (1 + args.profit_trigger_pct),
            position.initial_qty,
            position.remaining_qty,
            order_id,
        )
    LOG.warning(
        "Initial stop loss armed: SL=%.2f (%.2f%% below entry). If hit before profit trigger, all %s qty exits.",
        position.initial_stop_loss,
        args.initial_stop_loss_pct * 100,
        position.initial_qty,
    )
    return position


def exit_quantity(
    client: KiteClient,
    *,
    position: ManagedPosition,
    quantity: int,
    product: str,
    variety: str,
    tag: str,
    market_protection: float,
) -> str:
    return client.place_market_order(
        variety=variety,
        exchange=position.exchange,
        tradingsymbol=position.tradingsymbol,
        transaction_type="SELL",
        quantity=quantity,
        product=product,
        tag=tag,
        market_protection=market_protection,
    )


def close_position(
    client: KiteClient,
    *,
    position: ManagedPosition,
    quantity: int,
    exit_ltp: float,
    reason: str,
    args: argparse.Namespace,
) -> str:
    order_id = exit_quantity(
        client,
        position=position,
        quantity=quantity,
        product=args.product,
        variety=args.variety,
        tag=args.tag,
        market_protection=args.market_protection,
    )
    realized_pnl = round_price((exit_ltp - position.entry_price) * quantity)
    position.realized_pnl = round_price(position.realized_pnl + realized_pnl)
    today_pnl = record_daily_pnl(args.daily_pnl_file, realized_pnl)
    position.remaining_qty -= quantity
    if position.remaining_qty <= 0:
        position.remaining_qty = 0
        position.exit_reason = reason
    LOG.warning(
        "Exit fired: reason=%s ltp=%.2f order_id=%s exited_qty=%s realized_pnl=%.2f position_realized_pnl=%.2f today_recorded_pnl=%.2f remaining_qty=%s",
        reason,
        exit_ltp,
        order_id,
        quantity,
        realized_pnl,
        position.realized_pnl,
        today_pnl,
        position.remaining_qty,
    )
    return order_id


def manage_position(args: argparse.Namespace, position: Optional[ManagedPosition] = None) -> None:
    execute = ensure_live_confirmation(args)
    ensure_daily_loss_allowed(args)
    client = KiteClient(
        os.environ.get("KITE_API_KEY"),
        os.environ.get("KITE_ACCESS_TOKEN"),
        execute=execute,
    )
    position = position or load_position(args.state_file)
    if execute and position.entry_order_id.startswith("DRYRUN-"):
        raise SystemExit(
            "Refusing live manage for a dry-run/simulated position. "
            "Kite has no matching real position. Start a fresh live entry after confirming funds."
        )
    instrument = OptionInstrument(
        instrument_token=0,
        tradingsymbol=position.tradingsymbol,
        exchange=position.exchange,
        expiry=ist_now().date(),
        strike=0,
        lot_size=position.lot_size,
        instrument_type="CE" if position.signal == "buy" else "PE",
    )

    LOG.info(
        "Managing %s position: %s:%s entry=%.2f remaining_qty=%s partial_exit_done=%s poll_seconds=%.2f",
        "LIVE" if execute else "SIMULATED",
        position.exchange,
        position.tradingsymbol,
        position.entry_price,
        position.remaining_qty,
        position.partial_exit_done,
        args.poll_seconds,
    )

    tick = 0
    while position.remaining_qty > 0:
        tick += 1
        if args.max_daily_loss is not None and daily_pnl_for_today(args.daily_pnl_file) <= -abs(args.max_daily_loss):
            LOG.error("Daily loss guard reached while managing. Closing position immediately.")
            ltp = get_option_ltp(client, instrument, args.paper_option_ltp)
            close_position(
                client,
                position=position,
                quantity=position.remaining_qty,
                exit_ltp=ltp,
                reason="DAILY_LOSS_LIMIT",
                args=args,
            )
            save_position(position, args.state_file)
            break

        ltp = get_option_ltp(client, instrument, args.paper_option_ltp)
        position.highest_ltp = max(position.highest_ltp, ltp)
        trigger_price = round_price(position.entry_price * (1 + position.profit_trigger_pct))
        pnl_points = ltp - position.entry_price
        pnl_pct = pnl_points / position.entry_price if position.entry_price else 0.0
        trigger_gap = trigger_price - ltp

        if force_exit_due(args.force_exit_time):
            close_position(
                client,
                position=position,
                quantity=position.remaining_qty,
                exit_ltp=ltp,
                reason="FORCE_EXIT_TIME",
                args=args,
            )
            save_position(position, args.state_file)
            break

        if position.stop_loss is not None and ltp <= position.stop_loss:
            reason = "INITIAL_SL" if not position.partial_exit_done else "BREAKEVEN_SL"
            if position.partial_exit_done and position.stop_loss > position.entry_price:
                reason = "TRAILING_SL"
            close_position(
                client,
                position=position,
                quantity=position.remaining_qty,
                exit_ltp=ltp,
                reason=reason,
                args=args,
            )
            save_position(position, args.state_file)
            break

        if not position.partial_exit_done and ltp >= trigger_price:
            order_id = close_position(
                client,
                position=position,
                quantity=position.half_exit_qty,
                exit_ltp=ltp,
                reason="PROFIT_10_PERCENT_PARTIAL",
                args=args,
            )
            position.partial_exit_done = True
            position.stop_loss = position.entry_price
            LOG.warning(
                "Partial exit done at profit trigger: ltp=%.2f trigger=%.2f order_id=%s exited_qty=%s remaining_qty=%s remaining_sl_moved_to_entry=%.2f",
                ltp,
                trigger_price,
                order_id,
                position.half_exit_qty,
                position.remaining_qty,
                position.stop_loss,
            )

        if position.partial_exit_done:
            extra_move_after_trigger = max(0.0, position.highest_ltp - trigger_price)
            trailed_stop = position.entry_price + extra_move_after_trigger / 2.0
            old_stop_loss = position.stop_loss
            position.stop_loss = max(position.stop_loss or position.entry_price, trailed_stop)
            if old_stop_loss is None or position.stop_loss > old_stop_loss:
                LOG.info(
                    "Trailing stop updated: old_sl=%s new_sl=%.2f highest_ltp=%.2f trigger_price=%.2f extra_move_after_trigger=%.2f",
                    "-" if old_stop_loss is None else f"{old_stop_loss:.2f}",
                    position.stop_loss,
                    position.highest_ltp,
                    trigger_price,
                    extra_move_after_trigger,
                )

        save_position(position, args.state_file)
        LOG.info(
            "Tick %s | LTP=%.2f | PnL=%+.2f pts (%+.2f%%) | high=%.2f | trigger_gap=%+.2f | SL=%s | remaining=%s | realized_pnl=%.2f | exit_reason=%s",
            tick,
            ltp,
            pnl_points,
            pnl_pct * 100,
            position.highest_ltp,
            trigger_gap,
            "-" if position.stop_loss is None else f"{position.stop_loss:.2f}",
            position.remaining_qty,
            position.realized_pnl,
            position.exit_reason or "-",
        )

        if args.once:
            break
        time.sleep(args.poll_seconds)


def show_order_status(args: argparse.Namespace) -> None:
    if not args.order_id:
        raise SystemExit("Pass --order-id with order-status.")
    client = KiteClient(
        os.environ.get("KITE_API_KEY"),
        os.environ.get("KITE_ACCESS_TOKEN"),
        execute=False,
    )
    if not client.has_credentials:
        raise SystemExit("Set KITE_API_KEY and KITE_ACCESS_TOKEN first.")

    history = client.order_history(args.order_id)
    if not history:
        raise SystemExit(f"No order history found for {args.order_id}")

    LOG.info("Order history for %s has %s events", args.order_id, len(history))
    for index, event in enumerate(history, start=1):
        LOG.info("Order event %s | %s", index, order_rejection_detail(event))


def prompt_for_signal() -> str:
    print()
    print("NIFTY option algo is ready.")
    print("Type 1 and press Enter: BUY signal  -> CE buy")
    print("Type 2 and press Enter: SELL signal -> PE buy")
    print("Type m and press Enter: manage existing saved position")
    print("Type q and press Enter: quit")
    print()

    while True:
        choice = input("Your choice [1/2/m/q]: ").strip().lower()
        if choice == "1":
            return "buy"
        if choice == "2":
            return "sell"
        if choice == "m":
            return "manage"
        if choice == "q":
            print("Exited without placing an order.")
            raise SystemExit(0)
        print("Please type only 1, 2, m, or q.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kite Connect v3 NIFTY option buy/sell signal algo.",
    )
    parser.add_argument(
        "signal",
        nargs="?",
        choices=["buy", "sell", "manage", "order-status"],
        help="'buy' buys nearest upper NIFTY CE, 'sell' buys nearest lower NIFTY PE.",
    )
    parser.add_argument("--lots", type=int, default=FIXED_LOTS, help="Ignored; this strategy always buys exactly 2 lots.")
    parser.add_argument("--product", default="MIS", choices=["MIS", "NRML"], help="Kite product type.")
    parser.add_argument("--variety", default="regular", help="Kite order variety.")
    parser.add_argument("--tag", default="nifty50algo", help="Kite order tag, max 20 chars.")
    parser.add_argument(
        "--market-protection",
        type=float,
        default=-1,
        help="Market protection for MARKET orders. -1 means Kite auto protection; custom percent can be 0-100.",
    )
    parser.add_argument("--profit-trigger-pct", type=float, default=0.10, help="Partial exit trigger. Default 0.10 means +10%%.")
    parser.add_argument("--initial-stop-loss-pct", type=float, default=0.10, help="Initial SL below entry. Default 0.10 means -10%%.")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--order-timeout", type=int, default=20)
    parser.add_argument("--force-exit-time", type=parse_hhmm, default=parse_hhmm("15:25"), help="Exit open position at/after HH:MM IST. Default 15:25.")
    parser.add_argument("--max-daily-loss", type=float, help="Stop/close when recorded daily PnL is <= this loss amount, e.g. 3000.")
    parser.add_argument("--daily-pnl-file", type=Path, default=DEFAULT_DAILY_PNL_FILE)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--instrument-cache", type=Path, default=DEFAULT_INSTRUMENT_CACHE)
    parser.add_argument("--refresh-instruments", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Place real Kite orders.")
    parser.add_argument(
        "--i-understand-live-risk",
        action="store_true",
        help="Required together with --execute.",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="Only place entry and save state; do not manage the position loop.",
    )
    parser.add_argument("--once", action="store_true", help="Run one management tick and exit.")
    parser.add_argument("--order-id", help="Kite order ID to inspect with order-status.")
    parser.add_argument("--paper-nifty-ltp", type=float, help="Paper-test NIFTY spot LTP.")
    parser.add_argument("--paper-option-ltp", type=float, help="Paper-test option LTP.")
    parser.add_argument("--log-file", type=Path, help="Write detailed logs to this file.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Terminal log verbosity. File logs are always DEBUG.",
    )
    parser.add_argument("--log-max-bytes", type=int, default=2_000_000)
    parser.add_argument("--log-backup-count", type=int, default=5)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log_file = configure_logging(args)
    if args.signal is None:
        args.signal = prompt_for_signal()
        LOG.info("Interactive choice resolved to signal=%s", args.signal)

    execute = args.execute and args.i_understand_live_risk
    LOG.info("Script started at %s", ist_now().isoformat(timespec="seconds"))
    LOG.info(
        "Runtime config: signal=%s mode=%s state_file=%s instrument_cache=%s log_file=%s",
        args.signal,
        "LIVE" if execute else "DRY_RUN",
        args.state_file,
        args.instrument_cache,
        log_file,
    )
    LOG.info(
        "Credential check: KITE_API_KEY=%s KITE_ACCESS_TOKEN=%s",
        "present" if os.environ.get("KITE_API_KEY") else "missing",
        "present" if os.environ.get("KITE_ACCESS_TOKEN") else "missing",
    )

    if args.signal == "manage":
        manage_position(args)
        return 0
    if args.signal == "order-status":
        show_order_status(args)
        return 0

    position = start_position(args)
    if not args.no_monitor:
        manage_position(args, position)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOG.warning("Stopped by user.")
        raise SystemExit(130)
    except Exception:
        LOG.exception("Fatal error")
        raise SystemExit(1)
