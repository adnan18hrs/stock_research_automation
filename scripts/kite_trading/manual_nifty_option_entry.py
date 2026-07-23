#!/usr/bin/env python3
"""Manual NIFTY option entry with an immediately placed Kite stop loss.

Enter ``1`` only when you want to buy one lot of the NIFTY CE whose LTP is
closest to Rs. 100.  Enter ``2`` for the equivalent PE.  The script does not
manage or exit a position: exit remains entirely manual in Kite.

For a live run:
    python3 manual_nifty_option_entry.py --execute --i-understand-live-risk

Environment variables required for a live run:
    KITE_API_KEY
    KITE_ACCESS_TOKEN
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from nifty50_option_algo import (
    DEFAULT_INSTRUMENT_CACHE,
    KiteApiError,
    KiteClient,
    choose_option_by_target_price,
    configure_logging,
    get_nifty_ltp,
    ist_now,
    load_or_refresh_nfo_instruments,
    parse_nifty_options,
    selected_strike,
    wait_for_entry_price,
)


# The shared Kite helper configures this logger, so both entry and SL outcomes
# are written to the same per-run log file.
LOG = logging.getLogger("nifty50_option_algo")
SL_TRIGGER_FACTOR = 0.955  # 4.5% below actual buy average
SL_LIMIT_FACTOR = 0.95  # 5% below actual buy average
NFO_OPTION_TICK_SIZE = 0.05


def price_to_option_tick(price: float) -> float:
    """Round a price to a valid NFO option tick."""
    return round(round(price / NFO_OPTION_TICK_SIZE) * NFO_OPTION_TICK_SIZE, 2)


def stop_loss_prices(entry_price: float) -> tuple[float, float]:
    """Return valid-tick SELL SL trigger and limit prices for the entry."""
    trigger_price = price_to_option_tick(entry_price * SL_TRIGGER_FACTOR)
    limit_price = price_to_option_tick(entry_price * SL_LIMIT_FACTOR)
    if trigger_price >= entry_price:
        trigger_price = price_to_option_tick(entry_price - NFO_OPTION_TICK_SIZE)
    if limit_price >= trigger_price:
        limit_price = price_to_option_tick(trigger_price - NFO_OPTION_TICK_SIZE)
    if limit_price <= 0:
        raise ValueError(f"Cannot create a valid SL price from entry price {entry_price:.2f}")
    return trigger_price, limit_price


def place_stop_loss_limit_order(
    client: KiteClient,
    *,
    exchange: str,
    tradingsymbol: str,
    quantity: int,
    product: str,
    trigger_price: float,
    limit_price: float,
    tag: str,
) -> str:
    """Place the broker-side SELL SL order that protects the manual entry."""
    if limit_price >= trigger_price:
        raise ValueError("For a sell SL order, limit price must be below trigger price.")

    if not client.execute:
        LOG.info(
            "DRY RUN SL order: SELL %s %s:%s trigger=%.2f limit=%.2f product=%s",
            quantity,
            exchange,
            tradingsymbol,
            trigger_price,
            limit_price,
            product,
        )
        return "DRYRUN-SL"

    LOG.warning(
        "LIVE SL submit: SELL %s %s:%s trigger=%.2f limit=%.2f product=%s",
        quantity,
        exchange,
        tradingsymbol,
        trigger_price,
        limit_price,
        product,
    )
    data: Dict[str, Any] = client._request(  # KiteClient owns API authentication/request handling.
        "POST",
        "/orders/regular",
        params={
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": "SELL",
            "quantity": quantity,
            "product": product,
            "order_type": "SL",
            "price": limit_price,
            "trigger_price": trigger_price,
            "validity": "DAY",
            "tag": tag[:20],
        },
    )
    order_id = data.get("order_id")
    if not order_id:
        raise KiteApiError(f"SL order response did not include order_id: {data}")
    return str(order_id)


def prompt_for_signal() -> str:
    print("\nReady. The script will only place an entry after your choice.")
    print("1 = Buy 1 NIFTY CE lot (option LTP closest to Rs. 100)")
    print("2 = Buy 1 NIFTY PE lot (option LTP closest to Rs. 100)")
    print("q = Quit without any order\n")
    while True:
        choice = input("Your choice [1/2/q]: ").strip().lower()
        if choice == "1":
            return "buy"
        if choice == "2":
            return "sell"
        if choice == "q":
            print("Exited without placing an order.")
            raise SystemExit(0)
        print("Please type only 1, 2, or q.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual 1-lot NIFTY option entry plus Kite SL.")
    parser.add_argument("--product", default="MIS", choices=["MIS", "NRML"])
    parser.add_argument("--market-protection", type=float, default=-1)
    parser.add_argument("--order-timeout", type=int, default=20)
    parser.add_argument("--instrument-cache", type=Path, default=DEFAULT_INSTRUMENT_CACHE)
    parser.add_argument("--refresh-instruments", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Place real Kite orders.")
    parser.add_argument("--i-understand-live-risk", action="store_true", help="Required with --execute.")
    parser.add_argument("--paper-nifty-ltp", type=float, help="Paper-test NIFTY spot LTP.")
    parser.add_argument("--paper-option-ltp", type=float, help="Paper-test option LTP.")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-max-bytes", type=int, default=2_000_000)
    parser.add_argument("--log-backup-count", type=int, default=5)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.execute != args.i_understand_live_risk:
        raise SystemExit("Live orders require both --execute and --i-understand-live-risk.")

    # Reuse the existing project's logging setup without mixing logs with the algo manager.
    if args.log_file is None:
        args.log_file = Path(__file__).resolve().parents[2] / "logs" / f"manual_nifty_option_entry_{ist_now():%Y%m%d}.log"
    configure_logging(args)

    signal = prompt_for_signal()
    client = KiteClient(
        os.environ.get("KITE_API_KEY"),
        os.environ.get("KITE_ACCESS_TOKEN"),
        execute=args.execute,
    )
    if args.execute and not client.has_credentials:
        raise SystemExit("Set KITE_API_KEY and KITE_ACCESS_TOKEN before a live run.")

    LOG.info("Manual signal received: %s | mode=%s", signal, "LIVE" if args.execute else "DRY_RUN")
    nifty_ltp = get_nifty_ltp(client, args.paper_nifty_ltp)
    anchor_strike = selected_strike(signal, nifty_ltp)
    option, option_ltp = choose_option_by_target_price(
        client,
        parse_nifty_options(
            load_or_refresh_nfo_instruments(
                client, cache_file=args.instrument_cache, refresh=args.refresh_instruments
            )
        ),
        signal=signal,
        anchor_strike=anchor_strike,
        as_of=ist_now().date(),
        paper_option_ltp=args.paper_option_ltp,
    )

    # One lot means the current lot size from Kite's instrument master, never one unit.
    quantity = option.lot_size
    LOG.warning(
        "Entry: BUY %s %s:%s | NIFTY=%.2f | anchor=%s | option LTP=%.2f",
        quantity,
        option.exchange,
        option.tradingsymbol,
        nifty_ltp,
        anchor_strike,
        option_ltp,
    )
    entry_order_id = client.place_market_order(
        variety="regular",
        exchange=option.exchange,
        tradingsymbol=option.tradingsymbol,
        transaction_type="BUY",
        quantity=quantity,
        product=args.product,
        tag="manualniftyentry",
        market_protection=args.market_protection,
    )
    entry_price = wait_for_entry_price(
        client,
        order_id=entry_order_id,
        fallback_ltp=option_ltp,
        timeout_seconds=args.order_timeout,
    )

    trigger_price, limit_price = stop_loss_prices(entry_price)
    try:
        sl_order_id = place_stop_loss_limit_order(
            client,
            exchange=option.exchange,
            tradingsymbol=option.tradingsymbol,
            quantity=quantity,
            product=args.product,
            trigger_price=trigger_price,
            limit_price=limit_price,
            tag="manualniftysl",
        )
    except Exception:
        LOG.critical(
            "ENTRY IS OPEN BUT SL PLACEMENT FAILED. Immediately place a manual SELL SL "
            "for %s %s:%s: trigger=%.2f, limit=%.2f.",
            quantity,
            option.exchange,
            option.tradingsymbol,
            trigger_price,
            limit_price,
        )
        raise
    LOG.warning(
        "DONE: entry order=%s avg_price=%.2f | SL order=%s trigger=%.2f limit=%.2f. Exit remains manual.",
        entry_order_id,
        entry_price,
        sl_order_id,
        trigger_price,
        limit_price,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        LOG.exception("Entry/SL workflow failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
