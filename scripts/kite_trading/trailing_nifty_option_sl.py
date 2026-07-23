#!/usr/bin/env python3
"""Manual one-lot NIFTY entry with an immediately placed and trailing Kite SL.

Enter 1 for CE or 2 for PE. The script selects the nearby NIFTY option whose
LTP is closest to Rs. 100, buys one current lot, places a SELL SL, then raises
that exact SL as the option makes new highs. If the initial SL cannot be placed,
it immediately submits a protected SELL MARKET emergency exit for that one lot.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nifty50_option_algo import (
    DEFAULT_INSTRUMENT_CACHE,
    KiteApiError,
    KiteClient,
    choose_option_by_target_price,
    configure_logging,
    get_nifty_ltp,
    get_option_ltp,
    ist_now,
    load_or_refresh_nfo_instruments,
    parse_nifty_options,
    selected_strike,
    wait_for_entry_price,
)


LOG = logging.getLogger("nifty50_option_algo")
TICK_SIZE = 0.05
INITIAL_SL_LIMIT_FACTOR = 0.95
PHASE_ONE_END_FACTOR = 1.10
PHASE_ONE_GAP_FACTOR = 0.05
TRAIL_FACTOR_AFTER_10_PCT = 2.0 / 3.0
TRIGGER_GAP_FACTOR = 0.005
OPEN_SL_STATUSES = {"TRIGGER PENDING", "OPEN", "MODIFY PENDING", "MODIFY VALIDATION PENDING"}
TRANSIENT_SL_STATUSES = {"CANCEL PENDING", "CANCEL VALIDATION PENDING"}


def price_to_tick(price: float) -> float:
    return round(round(price / TICK_SIZE) * TICK_SIZE, 2)


def trailing_limit_price(entry_price: float, highest_ltp: float) -> float:
    """Return the limit SL from the requested two-stage trailing rule."""
    initial_limit = price_to_tick(entry_price * INITIAL_SL_LIMIT_FACTOR)
    if highest_ltp <= entry_price:
        return initial_limit
    phase_one_end = entry_price * PHASE_ONE_END_FACTOR
    if highest_ltp <= phase_one_end:
        return price_to_tick(highest_ltp - (entry_price * PHASE_ONE_GAP_FACTOR))

    at_phase_one_end = entry_price * (PHASE_ONE_END_FACTOR - PHASE_ONE_GAP_FACTOR)
    return price_to_tick(
        at_phase_one_end + (highest_ltp - phase_one_end) * TRAIL_FACTOR_AFTER_10_PCT
    )


def stop_prices(entry_price: float, highest_ltp: float) -> Tuple[float, float]:
    """Return a valid-tick SELL SL trigger and limit price."""
    limit_price = trailing_limit_price(entry_price, highest_ltp)
    trigger_price = price_to_tick(limit_price + entry_price * TRIGGER_GAP_FACTOR)
    if trigger_price >= highest_ltp:
        raise KiteApiError(
            f"Refusing SL update: trigger {trigger_price:.2f} must stay below current LTP "
            f"{highest_ltp:.2f}."
        )
    return trigger_price, limit_price


def latest_order(client: KiteClient, order_id: str) -> Dict[str, Any]:
    history = client.order_history(order_id)
    if not history:
        raise KiteApiError(f"No Kite order history found for {order_id}.")
    return history[-1]


def place_initial_sl(
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
    if not client.execute:
        LOG.info("DRY RUN initial SL: trigger=%.2f limit=%.2f", trigger_price, limit_price)
        return "DRYRUN-SL"
    data = client._request(
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
        raise KiteApiError(f"Initial SL response did not include order_id: {data}")
    return str(order_id)


def modify_sl_order(client: KiteClient, *, order_id: str, trigger_price: float, limit_price: float) -> str:
    if not client.execute:
        LOG.info("DRY RUN modify SL: order=%s trigger=%.2f limit=%.2f", order_id, trigger_price, limit_price)
        return "DRYRUN-MODIFY"
    data = client._request(
        "PUT",
        f"/orders/regular/{order_id}",
        params={
            "order_type": "SL",
            "price": limit_price,
            "trigger_price": trigger_price,
            "validity": "DAY",
        },
    )
    order_id = data.get("order_id")
    if not order_id:
        raise KiteApiError(f"SL modification response did not include order_id: {data}")
    return str(order_id)


def find_pending_sl_by_tag(
    client: KiteClient,
    *,
    exchange: str,
    tradingsymbol: str,
    quantity: int,
    product: str,
    tag: str,
) -> Optional[str]:
    """Check whether a timed-out SL request actually reached Kite.

    This prevents a duplicate SELL if Kite accepted the SL but the API response
    was lost before reaching this script.
    """
    orders = client._request("GET", "/orders")
    matches = [
        order
        for order in orders
        if order.get("exchange") == exchange
        and order.get("tradingsymbol") == tradingsymbol
        and int(order.get("quantity") or 0) == quantity
        and order.get("product") == product
        and order.get("transaction_type") == "SELL"
        and order.get("order_type") == "SL"
        and order.get("status") in OPEN_SL_STATUSES
        and (order.get("tag") == tag or tag in (order.get("tags") or []))
    ]
    if len(matches) > 1:
        raise KiteApiError(f"Found multiple pending SL orders with emergency-check tag {tag}.")
    return str(matches[0]["order_id"]) if matches else None


def submit_emergency_market_exit(
    client: KiteClient,
    *,
    exchange: str,
    tradingsymbol: str,
    quantity: int,
    product: str,
    market_protection: float,
) -> str:
    """Immediately submit a protected SELL MARKET order when no SL exists."""
    LOG.critical(
        "EMERGENCY EXIT: submitting SELL MARKET for %s %s:%s because initial SL failed.",
        quantity,
        exchange,
        tradingsymbol,
    )
    return client.place_market_order(
        variety="regular",
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        transaction_type="SELL",
        quantity=quantity,
        product=product,
        tag="niftyslemgexit",
        market_protection=market_protection,
    )


def wait_for_emergency_exit_completion(
    client: KiteClient,
    *,
    order_id: str,
    quantity: int,
    timeout_seconds: int,
) -> float:
    """Confirm that the emergency SELL order has fully executed at Kite."""
    if not client.execute:
        LOG.info("DRY RUN emergency exit treated as complete: order_id=%s", order_id)
        return 0.0

    deadline = time.monotonic() + timeout_seconds
    last_status = "UNKNOWN"
    last_filled_quantity = 0
    while time.monotonic() < deadline:
        order = latest_order(client, order_id)
        last_status = str(order.get("status") or "UNKNOWN")
        last_filled_quantity = int(order.get("filled_quantity") or 0)
        average_price = float(order.get("average_price") or 0)
        LOG.warning(
            "Emergency exit status: order=%s status=%s filled=%s/%s avg=%.2f",
            order_id,
            last_status,
            last_filled_quantity,
            quantity,
            average_price,
        )
        if last_status == "COMPLETE":
            if last_filled_quantity != quantity:
                raise KiteApiError(
                    f"Emergency exit {order_id} completed only {last_filled_quantity}/{quantity} quantity."
                )
            return average_price
        if last_status in {"REJECTED", "CANCELLED"}:
            detail = order.get("status_message") or order.get("status_message_raw") or "-"
            raise KiteApiError(f"Emergency exit {order_id} {last_status}: {detail}")
        time.sleep(1)
    raise KiteApiError(
        f"Emergency exit {order_id} was not confirmed within {timeout_seconds}s "
        f"(last status={last_status}, filled={last_filled_quantity}/{quantity})."
    )


def cancel_sl_and_confirm_terminal(
    client: KiteClient, *, order_id: str, timeout_seconds: int
) -> str:
    """Cancel an unfilled SL before a replacement market exit is sent."""
    try:
        client._request("DELETE", f"/orders/regular/{order_id}")
    except KiteApiError:
        # The SL can fill in the short interval before Kite processes cancellation.
        status_after_cancel_error = str(latest_order(client, order_id).get("status") or "UNKNOWN")
        if status_after_cancel_error in {"COMPLETE", "CANCELLED", "REJECTED"}:
            return status_after_cancel_error
        raise
    deadline = time.monotonic() + timeout_seconds
    last_status = "UNKNOWN"
    while time.monotonic() < deadline:
        last_status = str(latest_order(client, order_id).get("status") or "UNKNOWN")
        if last_status == "COMPLETE":
            return last_status
        if last_status in {"CANCELLED", "REJECTED"}:
            return last_status
        time.sleep(0.5)
    raise KiteApiError(
        f"SL cancellation for {order_id} was not confirmed within {timeout_seconds}s "
        f"(last status={last_status})."
    )


def net_position_quantity(
    client: KiteClient, *, exchange: str, tradingsymbol: str, product: str
) -> int:
    """Return the actual live net quantity before issuing any replacement exit."""
    positions = client._request("GET", "/portfolio/positions")
    matching = [
        item
        for item in positions.get("net", [])
        if item.get("exchange") == exchange
        and item.get("tradingsymbol") == tradingsymbol
        and item.get("product") == product
    ]
    return sum(int(item.get("quantity") or 0) for item in matching)


def emergency_exit_after_unfilled_sl(
    client: KiteClient,
    *,
    sl_order_id: str,
    sl_status: str,
    exchange: str,
    tradingsymbol: str,
    expected_quantity: int,
    product: str,
    market_protection: float,
    cancel_timeout_seconds: int,
    exit_timeout_seconds: int,
) -> bool:
    """Safely replace a failed/unfilled SL with a verified market exit.

    Returns True only when an emergency exit is confirmed complete. A pending
    SL is cancelled and its terminal state confirmed first, preventing a
    duplicate sell if it fills while the replacement is being prepared.
    """
    if sl_status in OPEN_SL_STATUSES:
        terminal_status = cancel_sl_and_confirm_terminal(
            client, order_id=sl_order_id, timeout_seconds=cancel_timeout_seconds
        )
        if terminal_status == "COMPLETE":
            LOG.warning("SL %s filled while cancelling; no emergency exit required.", sl_order_id)
            return False
    elif sl_status not in {"CANCELLED", "REJECTED"}:
        raise KiteApiError(f"Cannot safely replace SL {sl_order_id} with status={sl_status}.")

    quantity = net_position_quantity(
        client, exchange=exchange, tradingsymbol=tradingsymbol, product=product
    )
    if quantity == 0:
        LOG.warning("No open position remains for %s:%s; no emergency exit required.", exchange, tradingsymbol)
        return False
    if quantity != expected_quantity:
        raise KiteApiError(
            f"Expected one-lot long quantity {expected_quantity}, but live net position is {quantity}; "
            "refusing automatic exit."
        )
    emergency_order_id = submit_emergency_market_exit(
        client,
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        quantity=quantity,
        product=product,
        market_protection=market_protection,
    )
    average_price = wait_for_emergency_exit_completion(
        client,
        order_id=emergency_order_id,
        quantity=quantity,
        timeout_seconds=exit_timeout_seconds,
    )
    LOG.critical(
        "Emergency exit CONFIRMED COMPLETE: order=%s quantity=%s avg_price=%.2f.",
        emergency_order_id,
        quantity,
        average_price,
    )
    return True


def prompt_for_signal() -> str:
    print("\nReady. Entry will happen only after your choice.")
    print("1 = Buy 1 NIFTY CE lot and start trailing SL")
    print("2 = Buy 1 NIFTY PE lot and start trailing SL")
    print("q = Quit without any order\n")
    while True:
        choice = input("Your choice [1/2/q]: ").strip().lower()
        if choice == "1":
            return "buy"
        if choice == "2":
            return "sell"
        if choice == "q":
            raise SystemExit("Exited without placing an order.")
        print("Please type only 1, 2, or q.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual 1-lot NIFTY entry plus trailing Kite SL.")
    parser.add_argument("--product", default="MIS", choices=["MIS", "NRML"])
    parser.add_argument("--market-protection", type=float, default=-1)
    parser.add_argument("--order-timeout", type=int, default=20)
    parser.add_argument("--emergency-exit-timeout", type=int, default=20)
    parser.add_argument("--sl-cancel-timeout", type=int, default=5)
    parser.add_argument("--poll-seconds", type=float, default=1.0, help="LTP/SL check interval; minimum 1 second.")
    parser.add_argument("--instrument-cache", type=Path, default=DEFAULT_INSTRUMENT_CACHE)
    parser.add_argument("--refresh-instruments", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Place and modify real Kite orders.")
    parser.add_argument("--i-understand-live-risk", action="store_true", help="Required with --execute.")
    parser.add_argument("--paper-nifty-ltp", type=float)
    parser.add_argument("--paper-option-ltp", type=float)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-max-bytes", type=int, default=2_000_000)
    parser.add_argument("--log-backup-count", type=int, default=5)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.poll_seconds < 1:
        raise SystemExit("--poll-seconds must be at least 1 second.")
    if args.emergency_exit_timeout < 1:
        raise SystemExit("--emergency-exit-timeout must be at least 1 second.")
    if args.sl_cancel_timeout < 1:
        raise SystemExit("--sl-cancel-timeout must be at least 1 second.")
    if args.execute != args.i_understand_live_risk:
        raise SystemExit("Live trading requires both --execute and --i-understand-live-risk.")
    if args.log_file is None:
        args.log_file = Path(__file__).resolve().parents[2] / "logs" / f"trailing_nifty_option_sl_{ist_now():%Y%m%d}.log"
    configure_logging(args)

    signal = prompt_for_signal()
    client = KiteClient(os.environ.get("KITE_API_KEY"), os.environ.get("KITE_ACCESS_TOKEN"), execute=args.execute)
    if args.execute and not client.has_credentials:
        raise SystemExit("Set KITE_API_KEY and KITE_ACCESS_TOKEN before a live run.")

    nifty_ltp = get_nifty_ltp(client, args.paper_nifty_ltp)
    anchor_strike = selected_strike(signal, nifty_ltp)
    instrument, option_ltp = choose_option_by_target_price(
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
    quantity = instrument.lot_size  # exactly one current lot
    LOG.warning(
        "Entry chosen: BUY %s %s:%s | NIFTY=%.2f anchor=%s option LTP=%.2f",
        quantity, instrument.exchange, instrument.tradingsymbol, nifty_ltp, anchor_strike, option_ltp,
    )
    entry_order_id = client.place_market_order(
        variety="regular",
        exchange=instrument.exchange,
        tradingsymbol=instrument.tradingsymbol,
        transaction_type="BUY",
        quantity=quantity,
        product=args.product,
        tag="niftytrailingentry",
        market_protection=args.market_protection,
    )
    entry_price = wait_for_entry_price(
        client, order_id=entry_order_id, fallback_ltp=option_ltp, timeout_seconds=args.order_timeout
    )
    initial_trigger, initial_limit = stop_prices(entry_price, entry_price)
    sl_tag = f"nftsl{entry_order_id[-14:]}"
    try:
        sl_order_id = place_initial_sl(
            client,
            exchange=instrument.exchange,
            tradingsymbol=instrument.tradingsymbol,
            quantity=quantity,
            product=args.product,
            trigger_price=initial_trigger,
            limit_price=initial_limit,
            tag=sl_tag,
        )
    except Exception as sl_error:
        try:
            sl_order_id = find_pending_sl_by_tag(
                client,
                exchange=instrument.exchange,
                tradingsymbol=instrument.tradingsymbol,
                quantity=quantity,
                product=args.product,
                tag=sl_tag,
            )
        except Exception as lookup_error:
            LOG.critical(
                "INITIAL SL RESPONSE FAILED AND KITE ORDERBOOK CHECK FAILED. "
                "No emergency market exit was sent because SL status is unknown. "
                "Check Kite immediately. SL error=%s | lookup error=%s",
                sl_error,
                lookup_error,
            )
            raise KiteApiError("Initial SL status is unknown; manual Kite check required.") from lookup_error
        if sl_order_id:
            LOG.warning(
                "Initial SL request raised an error (%s), but Kite confirms the pending SL order=%s. "
                "No emergency exit submitted.",
                sl_error,
                sl_order_id,
            )
        else:
            try:
                emergency_order_id = submit_emergency_market_exit(
                    client,
                    exchange=instrument.exchange,
                    tradingsymbol=instrument.tradingsymbol,
                    quantity=quantity,
                    product=args.product,
                    market_protection=args.market_protection,
                )
            except Exception as emergency_error:
                LOG.critical(
                    "CRITICAL: INITIAL SL FAILED AND EMERGENCY EXIT ALSO FAILED. "
                    "Open position: BUY %s %s:%s. SL error=%s | emergency error=%s",
                    quantity,
                    instrument.exchange,
                    instrument.tradingsymbol,
                    sl_error,
                    emergency_error,
                )
                raise KiteApiError("Initial SL and emergency market exit both failed.") from emergency_error
            LOG.critical(
                "Initial SL failed (%s). Emergency SELL MARKET submitted: order_id=%s. "
                "Waiting for Kite fill confirmation.",
                sl_error,
                emergency_order_id,
            )
            try:
                emergency_exit_price = wait_for_emergency_exit_completion(
                    client,
                    order_id=emergency_order_id,
                    quantity=quantity,
                    timeout_seconds=args.emergency_exit_timeout,
                )
            except Exception as verification_error:
                LOG.critical(
                    "CRITICAL: EMERGENCY EXIT SUBMITTED BUT NOT CONFIRMED. "
                    "Check Kite immediately: order=%s, expected_qty=%s, error=%s",
                    emergency_order_id,
                    quantity,
                    verification_error,
                )
                raise
            LOG.critical(
                "Emergency exit CONFIRMED COMPLETE: order=%s quantity=%s avg_price=%.2f.",
                emergency_order_id,
                quantity,
                emergency_exit_price,
            )
            return 0
    LOG.warning(
        "Entry order=%s avg=%.2f | Kite SL order=%s trigger=%.2f limit=%.2f",
        entry_order_id, entry_price, sl_order_id, initial_trigger, initial_limit,
    )

    highest_ltp = entry_price
    current_limit = initial_limit
    while True:
        if client.execute:
            sl_order = latest_order(client, sl_order_id)
            status = str(sl_order.get("status") or "UNKNOWN")
            if status == "COMPLETE":
                LOG.warning("Stopping: Kite SL is complete; position has been exited.")
                return 0
            if status in TRANSIENT_SL_STATUSES:
                LOG.warning("SL has transient status=%s; waiting for Kite update.", status)
                time.sleep(args.poll_seconds)
                continue
        current_ltp = get_option_ltp(client, instrument, args.paper_option_ltp)
        live_trigger = float(sl_order.get("trigger_price") or 0) if client.execute else 0.0
        sl_needs_emergency_exit = (
            client.execute
            and (
                status in {"CANCELLED", "REJECTED"}
                or (status in OPEN_SL_STATUSES and live_trigger > 0 and current_ltp <= live_trigger)
            )
        )
        if sl_needs_emergency_exit:
            LOG.critical(
                "SL protection failed/unfilled: status=%s CP=%.2f trigger=%.2f. "
                "Cancelling any pending SL and preparing emergency exit.",
                status,
                current_ltp,
                live_trigger,
            )
            emergency_exit_after_unfilled_sl(
                client,
                sl_order_id=sl_order_id,
                sl_status=status,
                exchange=instrument.exchange,
                tradingsymbol=instrument.tradingsymbol,
                expected_quantity=quantity,
                product=args.product,
                market_protection=args.market_protection,
                cancel_timeout_seconds=args.sl_cancel_timeout,
                exit_timeout_seconds=args.emergency_exit_timeout,
            )
            return 0
        highest_ltp = max(highest_ltp, current_ltp)
        target_trigger, target_limit = stop_prices(entry_price, highest_ltp)
        if target_limit > current_limit:
            modified_id = modify_sl_order(
                client, order_id=sl_order_id, trigger_price=target_trigger, limit_price=target_limit
            )
            current_limit = target_limit
            LOG.warning(
                "SL raised: CP=%.2f high=%.2f -> trigger=%.2f limit=%.2f (order=%s)",
                current_ltp, highest_ltp, target_trigger, target_limit, modified_id,
            )
        else:
            LOG.info(
                "No SL change: CP=%.2f high=%.2f current_limit=%.2f target_limit=%.2f",
                current_ltp, highest_ltp, current_limit, target_limit,
            )
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOG.warning("Trailing stopped by user. The existing Kite SL remains active.")
        raise SystemExit(130)
    except Exception as exc:
        LOG.exception("Entry/trailing workflow failed; check the existing Kite SL immediately.")
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
