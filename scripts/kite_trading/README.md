# Kite Trading Scripts

This folder contains the Kite Connect trading helpers.

Run from this folder:

```bash
cd /Users/adnankhan/stock_research_automation/scripts/kite_trading
python3 kite_generate_access_token.py
python3 nifty50_option_algo.py --lots 2 --execute --i-understand-live-risk
```

## Manual 1-lot entry with broker-side SL

Use this when you want to decide every entry yourself. It does **not** monitor
or auto-exit the position. After you enter `1` (CE) or `2` (PE), it buys exactly
one current NIFTY lot using the existing option-selection logic: NIFTY is
anchored to the appropriate 100-point strike and nearby contracts are scanned
to choose the option whose LTP is closest to Rs. 100.

Once the market buy is confirmed, the script immediately places a Kite regular
SELL `SL` order for the same lot. For an actual entry average of Rs. 100, its
trigger is Rs. 95.50 and its limit price is Rs. 95.00.

```bash
cd /Users/adnankhan/stock_research_automation/scripts/kite_trading
python3 manual_nifty_option_entry.py --execute --i-understand-live-risk
```

Without those two live flags, the script remains dry-run only. Ensure your
Kite access token is current before a live run.

## Manual 1-lot entry with trailing SL

`trailing_nifty_option_sl.py` is standalone: do **not** run
`manual_nifty_option_entry.py` first and do not supply any order IDs. It asks
for `1` (CE) or `2` (PE), buys one lot, immediately adds the initial SELL `SL`,
and then updates that same SL upward in Kite. It never places an exit order and
never moves the SL down. The only exception is safety: if the initial SL API
request fails after the entry is filled, it immediately submits a protected
SELL MARKET emergency exit for the same one lot and polls Kite until it is
confirmed `COMPLETE`. A rejection, partial fill, cancellation, or a 20-second
confirmation timeout produces a critical alert:

```bash
python3 trailing_nifty_option_sl.py --execute --i-understand-live-risk
```

`Ctrl-C` stops the script but leaves the last SL active at Kite.

If a triggered SELL `SL` remains unfilled (`OPEN`) while the option LTP is at
or below its trigger, or if Kite marks the SL `CANCELLED`/`REJECTED`, the script
cancels any still-pending SL, verifies that one long lot is actually open, and
then submits and confirms a protected SELL MARKET emergency exit. It does not
send that market order if the SL cancellation/status is uncertain, preventing a
duplicate sell.

For an entry of Rs. 100, SL **limit** prices follow this path (the trigger is
Rs. 0.50 above the limit):

- CP Rs. 102 -> limit SL Rs. 97
- CP Rs. 105 -> limit SL Rs. 100
- CP Rs. 110 -> limit SL Rs. 105
- Above CP Rs. 110, the limit trails by two-thirds of each additional point:
  CP Rs. 113 -> limit SL Rs. 107.

Interactive choices in `nifty50_option_algo.py`:

- `1` = CE buy
- `2` = PE buy
- `m` = manage saved position
- `q` = quit

Logs and state are still stored in:

```text
/Users/adnankhan/stock_research_automation/logs
```

Risk rules in `nifty50_option_algo.py`:

- Always buys exactly 2 lots for `1` / `2` signals.
- First anchors to the nearest valid 100-point NIFTY strike: `1` uses CE above spot, `2` uses PE below spot.
- Before entry, scans nearby strikes from `anchor - 150` to `anchor + 150` in 50-point steps.
- From those candidates, buys the same CE/PE expiry whose LTP is closest to 100.
- Initial stop loss is 5% below entry; if hit before target, both lots exit.
- At +10% option profit, 1 lot exits.
- Remaining 1 lot stop loss moves to entry price.
- After +10%, trailing SL moves up by 2/3 of the extra move above the +10% trigger.
- Default MIS force-exit time is 15:25 IST.
- Optional daily loss guard:

```bash
python3 nifty50_option_algo.py --max-daily-loss 3000 --execute --i-understand-live-risk
```
