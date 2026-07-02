# Kite Trading Scripts

This folder contains the Kite Connect trading helpers.

Run from this folder:

```bash
cd /Users/adnankhan/stock_research_automation/scripts/kite_trading
python3 kite_generate_access_token.py
python3 nifty50_option_algo.py --lots 2 --execute --i-understand-live-risk
```

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
- Initial stop loss is 10% below entry; if hit before target, both lots exit.
- At +10% option profit, 1 lot exits.
- Remaining 1 lot stop loss moves to entry price.
- After +10%, trailing SL moves up by half of the extra move above the +10% trigger.
- Default MIS force-exit time is 15:25 IST.
- Optional daily loss guard:

```bash
python3 nifty50_option_algo.py --max-daily-loss 3000 --execute --i-understand-live-risk
```
