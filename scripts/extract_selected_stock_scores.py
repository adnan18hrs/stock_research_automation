import argparse
import re
from pathlib import Path


DEFAULT_TICKERS = [
    "CUMMINSIND",
    "ABB",
    "NETWEB",
    "ZENTEC",
    "HBLENGINE",
    "WAAREEENER",
    "POWERINDIA",
    "MOSCHIP",
    "KAYNES",
    "CAPLIPOINT",
]

FACTORS = [
    ("business", "Business Quality Score"),
    ("management", "Management Quality Score"),
    ("financial", "Financial Strength Score"),
    ("valuation", "Valuation Score"),
    ("growth", "Growth Visibility Score"),
    ("governance", "Governance Score"),
]


def clean_text(value):
    value = re.sub(r"\*\*", "", value)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def normalize_label(label):
    label = clean_text(label).lower()
    label = re.sub(r"score", "", label)
    label = re.sub(r"[^a-z ]+", " ", label)
    return re.sub(r"\s+", " ", label).strip()


def normalize_header(label):
    label = clean_text(label).lower()
    label = re.sub(r"[^a-z ]+", " ", label)
    return re.sub(r"\s+", " ", label).strip()


def factor_key(label):
    label = normalize_label(label)

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


def score_column_index(header_cells):
    first_header = normalize_label(header_cells[0])
    if first_header not in {"category", "factor", "parameter", "metric"}:
        return None

    for index, cell in enumerate(header_cells):
        header = normalize_header(cell)
        if "score" in header and "weight" not in header and "weighted" not in header:
            return index

    return None


def extract_score(value):
    value = clean_text(value)
    match = re.search(r"N/?A|-?\d+(?:\.\d+)?(?:\s*/\s*10)?", value, flags=re.IGNORECASE)
    if not match:
        return "N/A"

    score = match.group(0).upper().replace(" ", "")
    if score in {"NA", "N/A"}:
        return "N/A"

    number = re.search(r"-?\d+(?:\.\d+)?", score).group(0)
    if score.endswith("/10"):
        return score
    return f"{number}/10"


def final_summary_section(markdown):
    matches = list(
        re.finditer(
            r"(?im)^#{1,6}\s*(?:\d+[.)]?\s*)?final summary table\b.*$",
            markdown,
        )
    )
    if not matches:
        return markdown

    section = markdown[matches[-1].start() :]
    next_heading = re.search(r"(?im)\n#{1,6}\s+(?!.*final summary table).+", section[1:])
    if next_heading:
        return section[: next_heading.start() + 1]
    return section


def parse_scores(report_path):
    markdown = report_path.read_text(encoding="utf-8", errors="ignore")
    section = final_summary_section(markdown)
    scores = {}
    score_index = None

    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            score_index = None
            continue

        cells = [clean_text(cell) for cell in line.strip("|").split("|")]
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

        key = factor_key(cells[0])
        if key:
            scores[key] = extract_score(cells[score_index])

    return scores


def markdown_table(rows):
    factor_headers = [label for _, label in FACTORS]
    lines = [
        "| Ticker | " + " | ".join(factor_headers) + " |",
        "|---|" + "|".join(["---:"] * len(factor_headers)) + "|",
    ]

    for ticker, scores in rows:
        values = [scores.get(key, "N/A") for key, _ in FACTORS]
        lines.append("| " + ticker + " | " + " | ".join(values) + " |")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Extract 6-factor scores from data/<TICKER>/investment_analysis.md "
            "for selected tickers. Missing tickers are ignored."
        )
    )
    parser.add_argument("tickers", nargs="*", default=DEFAULT_TICKERS)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", "-o", help="Optional markdown output file path")
    parser.add_argument(
        "--show-missing",
        action="store_true",
        help="Print tickers whose investment_analysis.md file was not found",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    rows = []
    missing = []

    for ticker in [ticker.upper() for ticker in args.tickers]:
        report_path = data_dir / ticker / "investment_analysis.md"
        if not report_path.exists():
            missing.append(ticker)
            continue

        rows.append((ticker, parse_scores(report_path)))

    output = markdown_table(rows) if rows else "No matching ticker reports found."

    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")

    print(output)

    if args.show_missing and missing:
        print()
        print("Missing/ignored tickers: " + ", ".join(missing))


if __name__ == "__main__":
    main()
