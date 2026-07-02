import json
import shutil
from datetime import datetime
from pathlib import Path


DATA_DIR = Path("data")
TEST_SYMBOLS = [
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
    "WIPRO",
    "ITC",
]


def delete_file(path, actions):
    if path.exists():
        path.unlink()
        actions.append({"action": "delete_file", "path": str(path)})
    else:
        actions.append({"action": "missing_file", "path": str(path)})


def delete_dir(path, actions):
    if path.exists():
        shutil.rmtree(path)
        actions.append({"action": "delete_dir", "path": str(path)})
    else:
        actions.append({"action": "missing_dir", "path": str(path)})


def latest_annual_report(symbol):
    annual_dir = DATA_DIR / symbol / "annual_reports"
    reports = sorted(annual_dir.glob("*.pdf"))
    return reports[-1] if reports else None


def main():
    actions = []

    reliance_latest = latest_annual_report("RELIANCE")
    if reliance_latest:
        delete_file(reliance_latest, actions)
    else:
        actions.append({"action": "no_annual_report_found", "symbol": "RELIANCE"})

    delete_file(DATA_DIR / "TCS" / "investment_analysis.md", actions)

    delete_dir(DATA_DIR / "INFY" / "concalls", actions)

    delete_file(DATA_DIR / "HDFCBANK" / "news" / "news_links.txt", actions)
    delete_file(DATA_DIR / "HDFCBANK" / "news" / "news_links.json", actions)

    wipro_latest = latest_annual_report("WIPRO")
    if wipro_latest:
        delete_file(wipro_latest, actions)
    else:
        actions.append({"action": "no_annual_report_found", "symbol": "WIPRO"})

    delete_dir(DATA_DIR / "WIPRO" / "concalls", actions)

    actions.append({"action": "left_untouched", "symbol": "ITC"})

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "test_symbols": TEST_SYMBOLS,
        "actions": actions,
    }

    manifest_path = DATA_DIR / "pipeline_recovery_test_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
