import argparse
import re
from pathlib import Path


METRICS = {
    "business": "Business Quality",
    "management": "Management Quality",
    "financial": "Financial Strength",
    "valuation": "Valuation",
    "growth": "Growth Visibility",
    "governance": "Governance",
}

FINANCIAL_MIN = 5.5


def normalize_metric(label):
    label = label.lower()
    label = re.sub(r"\*\*", "", label)
    label = re.sub(r"score", "", label)
    label = re.sub(r"[^a-z ]+", " ", label)
    label = re.sub(r"\s+", " ", label).strip()

    if "business" in label and "quality" in label:
        return "business"
    if "management" in label and "quality" in label:
        return "management"
    if "financial" in label and "strength" in label:
        return "financial"
    if "valuation" in label:
        return "valuation"
    if "growth" in label and "visibility" in label:
        return "growth"
    if "governance" in label:
        return "governance"

    return None


def normalize_header(label):
    label = label.lower()
    label = re.sub(r"\*\*", "", label)
    label = re.sub(r"[^a-z ]+", " ", label)
    return re.sub(r"\s+", " ", label).strip()


def score_column_index(cells):
    first_header = normalize_header(cells[0])
    if first_header not in {"category", "factor", "parameter", "metric"}:
        return None

    for index, cell in enumerate(cells):
        header = normalize_header(cell)
        if "score" in header and "weight" not in header and "weighted" not in header:
            return index

    return None


def extract_score(value):
    value = re.sub(r"\*\*", "", value)
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    return float(match.group(0))


def final_summary_text(markdown):
    matches = list(re.finditer(r"(?im)^#{1,6}\s*(?:\d+\.\s*)?final summary table\b.*$", markdown))
    if not matches:
        return markdown

    start = matches[-1].start()
    section = markdown[start:]

    next_heading = re.search(r"(?im)\n#{1,6}\s+(?!.*final summary table).+", section[1:])
    if next_heading:
        return section[: next_heading.start() + 1]

    return section


def parse_scores(path):
    markdown = path.read_text(encoding="utf-8", errors="ignore")
    section = final_summary_text(markdown)
    scores = {}
    score_index = None

    for raw_line in section.splitlines():
        line = raw_line.strip()

        if not line.startswith("|"):
            score_index = None
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]

        if len(cells) < 2:
            continue

        if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue

        current_score_index = score_column_index(cells)
        if current_score_index is not None:
            score_index = current_score_index
            continue

        if score_index is None or score_index >= len(cells):
            continue

        metric = normalize_metric(cells[0])
        if not metric:
            continue

        score = extract_score(cells[score_index])
        if score is None:
            continue

        scores[metric] = score

    missing = [name for name in METRICS if name not in scores]
    return scores, missing


def passes_filter(scores):
    return scores["financial"] < FINANCIAL_MIN


def format_score(value):
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Filter ticker investment_analysis.md reports where Financial Strength "
            f"is less than {FINANCIAL_MIN}, "
            "sorted by summed total score."
        )
    )
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    report_paths = sorted(data_dir.glob("*/investment_analysis.md"))
    rows = []
    skipped = []

    for report_path in report_paths:
        ticker = report_path.parent.name
        scores, missing = parse_scores(report_path)

        if missing:
            skipped.append((ticker, ", ".join(METRICS[item] for item in missing)))
            continue

        if not passes_filter(scores):
            continue

        total = sum(scores.values())
        rows.append((total, ticker, scores))

    rows.sort(key=lambda row: (-row[0], row[1]))

    print(
        f"Tickers with Financial Strength < {FINANCIAL_MIN}, "
        "sorted by total score"
    )
    print()
    if rows:
        print("| Rank | Ticker | Total Score | Business | Management | Financial | Valuation | Growth Visibility | Governance |")
        print("|---:|---|---:|---:|---:|---:|---:|---:|---:|")

        for rank, (total, ticker, scores) in enumerate(rows, start=1):
            print(
                "| "
                f"{rank} | {ticker} | {format_score(total)} | "
                f"{format_score(scores['business'])} | "
                f"{format_score(scores['management'])} | "
                f"{format_score(scores['financial'])} | "
                f"{format_score(scores['valuation'])} | "
                f"{format_score(scores['growth'])} | "
                f"{format_score(scores['governance'])} |"
            )
    else:
        print("No tickers matched the filter.")

    print()
    print(f"Reports checked: {len(report_paths)}")
    print(f"Passed: {len(rows)}")
    print(f"Skipped due to missing scores: {len(skipped)}")

    if skipped:
        print()
        print("Skipped tickers:")
        for ticker, missing in skipped:
            print(f"- {ticker}: {missing}")


if __name__ == "__main__":
    main()
