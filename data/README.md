# Local Research Data

This folder is intentionally kept out of git because the generated corpus can become very large.

The pipeline recreates ticker folders under `data/<SYMBOL>/` with subfolders such as:

- `annual_reports/`
- `screener_finance/`
- `concalls/`
- `news/`
- `research_cache/`
- `investment_analysis.md`

Run the pipeline from the project root to rebuild the local data:

```bash
python3 scripts/run_stock_pipeline.py --mode 2 --manual-symbols TCS INFY --dry-run --save-evidence
```

Remove `--dry-run` when you want the AI report to be generated.
