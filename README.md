# Stock Research Automation

Automated Indian equity research workspace for collecting public company material, building evidence packs, generating AI-backed investment reports, filtering score tables, and optionally running a Kite Connect NIFTY option execution helper.

The repository is code-first. Large generated folders such as `data/`, `logs/`, `venv/`, and secret `.env` files are intentionally not committed. Rebuild them locally using the commands below.

> This project is for research and automation support only. It is not financial advice. The Kite scripts can place real orders only when explicitly run with live flags; use them carefully.

## What This Project Does

1. Downloads public stock research inputs:
   - NSE annual reports
   - Screener company page HTML
   - Screener concall transcript PDFs
   - Google/DDGS news links
2. Builds an evidence pack per ticker from local files.
3. Sends that evidence pack to either:
   - Codex CLI through the Codex desktop app login, or
   - OpenAI Responses API using `OPENAI_API_KEY`
4. Writes `data/<SYMBOL>/investment_analysis.md`.
5. Parses final score tables and filters companies by financial-strength score.
6. Provides a separate Kite Connect helper for NIFTY option entry/management.

## Project Layout

```text
.
|-- config/
|   |-- nifty100.csv
|   |-- ind_nifty500list.csv
|   |-- ind_niftysmallcap250list.csv
|   `-- investment_analysis_prompt.md
|-- data/
|   `-- README.md
|-- logs/
|-- scripts/
|   |-- run_stock_pipeline.py
|   |-- AI_analysis_codex_on_ticker.py
|   |-- filter_investment_reports_on_score.py
|   |-- build_research_cache.py
|   |-- download_*.py
|   |-- extract_selected_stock_scores.py
|   `-- kite_trading/
|       |-- kite_generate_access_token.py
|       |-- nifty50_option_algo.py
|       `-- README.md
|-- visitor_link_site/
|-- .env.example
|-- requirements.txt
`-- selected_stock_scores.md
```

## Environment Used

The local virtual environment found in this workspace was Python `3.9.6`.

Recommended setup:

```bash
cd /Users/adnankhan/stock_research_automation
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Optional, only for Playwright test/helper scripts:

```bash
python3 -m playwright install
```

Create local secrets:

```bash
cp .env.example .env
```

Then edit `.env` or export variables in your terminal. Never commit real keys.

Important env vars:

- `OPENAI_API_KEY`: Required only for `--backend openai-api`.
- `OPENAI_MODEL`: Optional model override for OpenAI/Codex calls.
- `CODEX_BIN`: Codex CLI path. Default is `/Applications/Codex.app/Contents/Resources/codex`.
- `KITE_API_KEY`: Zerodha Kite Connect API key.
- `KITE_API_SECRET`: Required to generate a Kite access token.
- `KITE_ACCESS_TOKEN`: Daily/session token used by trading script.

## Main Workflow

Use `run_stock_pipeline.py` when you want the full refresh plus AI report flow.

CSV mode, using `config/nifty100.csv`:

```bash
python3 scripts/run_stock_pipeline.py --mode 1
```

Manual tickers:

```bash
python3 scripts/run_stock_pipeline.py --mode 2 --manual-symbols TCS INFY HDFCBANK
```

Dry run, useful before spending AI tokens/time:

```bash
python3 scripts/run_stock_pipeline.py --mode 2 --manual-symbols TCS --dry-run --save-evidence
```

Force refresh data and regenerate analysis:

```bash
python3 scripts/run_stock_pipeline.py --mode 2 --manual-symbols TCS --force-data --force-analysis
```

Skip AI and only refresh/audit source data:

```bash
python3 scripts/run_stock_pipeline.py --mode 2 --manual-symbols TCS --skip-analysis
```

What it writes:

- `data/<SYMBOL>/annual_reports/*.pdf`
- `data/<SYMBOL>/screener_finance/company_page.html`
- `data/<SYMBOL>/concalls/*.pdf`
- `data/<SYMBOL>/news/news_links.txt`
- `data/<SYMBOL>/news/news_links.json`
- `data/<SYMBOL>/investment_analysis.md`

Useful options:

- `--max-annual-reports`: Default `5`.
- `--max-transcripts`: Default `10`.
- `--max-news-links`: Default `15`.
- `--screener-max-age-days`: Default `7`.
- `--news-max-age-days`: Default `1`.
- `--evidence-chars`: Default `220000`.
- `--per-file-chars`: Default `25000`.
- `--codex-timeout`: Default `1800` seconds per ticker.
- `--disable-news-fallback`: Do not use the DDGS fallback.
- `--skip-news`: Keep existing news files unchanged.

## AI Report Script

Use `AI_analysis_codex_on_ticker.py` when data already exists and you only want to generate or regenerate `investment_analysis.md`.

Codex desktop backend:

```bash
python3 scripts/AI_analysis_codex_on_ticker.py --symbols TCS INFY --backend codex-cli
```

OpenAI API backend:

```bash
export OPENAI_API_KEY="your_key"
python3 scripts/AI_analysis_codex_on_ticker.py --symbols TCS --backend openai-api --model gpt-4.1
```

Dry run evidence pack only:

```bash
python3 scripts/AI_analysis_codex_on_ticker.py --symbols TCS --dry-run --save-evidence
```

When to use:

- Use this after data files are already downloaded.
- Use `--force` to overwrite an existing `investment_analysis.md`.
- Use `--save-evidence` when you want to inspect what was sent to the model.

## Score Filtering

`filter_investment_reports_on_score.py` reads every `data/*/investment_analysis.md`, parses the final summary table, and prints tickers where `Financial Strength < 5.5`, sorted by total score.

Run:

```bash
python3 scripts/filter_investment_reports_on_score.py
```

Custom data folder:

```bash
python3 scripts/filter_investment_reports_on_score.py --data-dir data
```

The parser expects score labels like:

- Business Quality
- Management Quality
- Financial Strength
- Valuation
- Growth Visibility
- Governance

## Kite Trading

Kite scripts live in `scripts/kite_trading/`.

First generate a daily/session access token:

```bash
cd scripts/kite_trading
export KITE_API_KEY="your_kite_key"
export KITE_API_SECRET="your_kite_secret"
python3 kite_generate_access_token.py
```

The Kite app redirect URL must match:

```text
http://127.0.0.1:8000/kite/callback
```

After login, export the printed token:

```bash
export KITE_ACCESS_TOKEN="token_printed_by_script"
```

Dry-run interactive mode:

```bash
python3 nifty50_option_algo.py
```

Live order mode requires both flags:

```bash
python3 nifty50_option_algo.py --execute --i-understand-live-risk
```

Direct signal commands:

```bash
python3 nifty50_option_algo.py buy --execute --i-understand-live-risk
python3 nifty50_option_algo.py sell --execute --i-understand-live-risk
python3 nifty50_option_algo.py manage --execute --i-understand-live-risk
```

Order status:

```bash
python3 nifty50_option_algo.py order-status --order-id YOUR_ORDER_ID
```

Strategy behavior:

- `buy` buys nearest upper NIFTY CE strike rounded to 100.
- `sell` buys nearest lower NIFTY PE strike rounded to 100.
- Script currently forces exactly 2 lots.
- Initial stop loss default is 10 percent below entry.
- At 10 percent option profit, half quantity exits.
- Remaining stop loss moves to entry, then trails upward.
- Default MIS force-exit time is `15:25` IST.
- Logs/state are written under root `logs/`.

Useful safety flags:

```bash
python3 nifty50_option_algo.py buy --max-daily-loss 3000 --execute --i-understand-live-risk
python3 nifty50_option_algo.py manage --once --execute --i-understand-live-risk
python3 nifty50_option_algo.py buy --paper-nifty-ltp 24000 --paper-option-ltp 120
```

## Other Scripts

- `build_research_cache.py`: Builds JSON caches from annual reports, concalls, Screener HTML, and news links. Useful when repeated analysis should avoid reparsing PDFs.
- `download_annual_reports_working.py`: Older standalone annual-report downloader.
- `download_screener_homePage_working.py`: Standalone Screener company-page downloader.
- `download_screener_transcripts.py`: Parses Screener transcript links and downloads latest raw transcript PDFs.
- `download_news_links_working.py`: Downloads Google/DDGS news links per ticker.
- `download_announcements_working.py`: Standalone announcement downloader.
- `extract_selected_stock_scores.py`: Extracts selected ticker score tables into markdown.
- `prepare_pipeline_recovery_test.py`: Deletes selected local artifacts to test recovery behavior.
- `test_*.py`: Local API/browser experiments and smoke checks.

## Visitor Link Site

`visitor_link_site/` is a separate consent-based product link/analytics site. It logs visitor/click events and can email alerts using SMTP.

Run:

```bash
cd visitor_link_site
cp .env.example .env
python3 server.py
```

Open:

```text
http://127.0.0.1:8080
```

Events are saved locally under `visitor_link_site/data/`, which is ignored by git.

## Data And Git Notes

This local workspace had a very large generated `data/` folder, so the repository ignores generated research data by default. Commit code, prompts, configs, and docs; regenerate data locally whenever needed.

Ignored by git:

- `.env` and `.env.*`
- `venv/` and `.venv/`
- `data/*` except `data/README.md`
- `logs/*` except `logs/.gitkeep`
- `visitor_link_site/data/`
- Python cache files and `.DS_Store`

If you need to preserve the full 17GB local research corpus, use an external archive, cloud bucket, or Git LFS with a deliberate storage plan instead of normal git commits.
