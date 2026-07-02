import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

try:
    import fitz
except ImportError:
    fitz = None


BASE_DIR = Path("data")
PROMPT_FILE = Path("config/investment_analysis_prompt.md")
DEFAULT_OUTPUT = "investment_analysis.md"
CODEX_BIN = "/Applications/Codex.app/Contents/Resources/codex"

SECTION_LIMITS = {
    "screener_finance": 30000,
    "annual_reports": 80000,
    "investor_presentations": 45000,
    "concalls": 60000,
    "concall_transcripts": 60000,
    "concall_updates": 35000,
    "announcements": 30000,
    "news": 20000,
}

SECTION_ORDER = [
    "screener_finance",
    "annual_reports",
    "investor_presentations",
    "concalls",
    "concall_transcripts",
    "concall_updates",
    "announcements",
    "news",
]

SUPPORTED_EXTENSIONS = {
    ".html",
    ".htm",
    ".json",
    ".md",
    ".pdf",
    ".txt",
}


def clean_text(text):
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate(text, limit):
    if len(text) <= limit:
        return text
    suffix = "\n\n[TRUNCATED]"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def read_pdf(path, max_chars):
    if fitz is None:
        return "[PDF text extraction unavailable: PyMuPDF/fitz is not installed.]"

    chunks = []
    used = 0

    try:
        with fitz.open(path) as doc:
            for page_no, page in enumerate(doc, start=1):
                page_text = clean_text(page.get_text("text"))
                if not page_text:
                    continue

                page_chunk = f"\n\n[Page {page_no}]\n{page_text}"
                remaining = max_chars - used

                if remaining <= 0:
                    break

                chunks.append(page_chunk[:remaining])
                used += min(len(page_chunk), remaining)

                if used >= max_chars:
                    break

    except Exception as exc:
        return f"[Could not extract PDF text: {exc}]"

    return clean_text("\n".join(chunks))


def read_html(path):
    raw = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(raw, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    return clean_text(soup.get_text("\n"))


def read_json(path):
    raw = path.read_text(encoding="utf-8", errors="ignore")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return clean_text(raw)

    return json.dumps(data, indent=2, ensure_ascii=False)


def read_file(path, max_chars):
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return read_pdf(path, max_chars)

    if suffix in {".html", ".htm"}:
        return truncate(read_html(path), max_chars)

    if suffix == ".json":
        return truncate(read_json(path), max_chars)

    return truncate(path.read_text(encoding="utf-8", errors="ignore"), max_chars)


def sort_key(path):
    stat = path.stat()
    return (stat.st_mtime, path.name)


def iter_section_files(section_dir):
    files = [
        path
        for path in section_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not path.name.startswith(".")
    ]
    return sorted(files, key=sort_key, reverse=True)


def build_section(section_name, section_dir, section_limit, per_file_limit):
    if not section_dir.exists():
        return f"## {section_name}\n\n[Folder missing]\n"

    files = iter_section_files(section_dir)

    if not files:
        return f"## {section_name}\n\n[No supported files found]\n"

    remaining = section_limit
    parts = [f"## {section_name}\n"]

    for path in files:
        if remaining <= 0:
            break

        budget = min(per_file_limit, remaining)
        text = read_file(path, budget)

        if not text:
            continue

        relative = path.relative_to(section_dir.parent)
        block = f"\n### Source: {relative}\n\n{text}\n"
        parts.append(block[:remaining])
        remaining -= min(len(block), remaining)

    if remaining <= 0:
        parts.append("\n[SECTION TRUNCATED]\n")

    return "\n".join(parts)


def build_evidence_pack(ticker_dir, total_limit, per_file_limit):
    ticker = ticker_dir.name
    parts = [
        f"# Evidence Pack: {ticker}",
        f"Generated on: {date.today().isoformat()}",
        "",
        "Use this evidence pack as the source material for the investment analysis.",
        "If a required datapoint is absent, mention that the local evidence does not contain it.",
        "",
    ]

    remaining = total_limit

    for section_name in SECTION_ORDER:
        if remaining <= 0:
            break

        section_limit = min(SECTION_LIMITS[section_name], remaining)
        section_text = build_section(
            section_name=section_name,
            section_dir=ticker_dir / section_name,
            section_limit=section_limit,
            per_file_limit=per_file_limit,
        )
        parts.append(section_text)
        remaining -= len(section_text)

    evidence = "\n".join(parts)
    return truncate(evidence, total_limit)


def discover_tickers(base_dir):
    if not base_dir.exists():
        raise FileNotFoundError(f"Data directory not found: {base_dir}")

    return sorted(
        path
        for path in base_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )


def parse_symbols(symbols):
    if not symbols:
        return None

    selected = set()

    for item in symbols:
        for symbol in item.split(","):
            symbol = symbol.strip().upper()
            if symbol:
                selected.add(symbol)

    return selected


def extract_response_text(response):
    if "output_text" in response:
        return response["output_text"].strip()

    chunks = []

    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if text:
                    chunks.append(text)

    return "\n".join(chunks).strip()


def call_openai(prompt, evidence, model, max_output_tokens, temperature):
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is not set.")

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    url = f"{base_url.rstrip('/')}/responses"

    payload = {
        "model": model,
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
        "input": [
            {
                "role": "system",
                "content": (
                    "You are a rigorous Indian equity research analyst. "
                    "Use only supplied evidence plus clearly labelled general industry knowledge. "
                    "Do not fabricate numbers, quotes, or source references."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"{prompt}\n\n"
                    "COMPANY EVIDENCE PACK STARTS BELOW\n\n"
                    f"{evidence}\n\n"
                    "COMPANY EVIDENCE PACK ENDS"
                ),
            },
        ],
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc

    data = json.loads(body)
    text = extract_response_text(data)

    if not text:
        raise RuntimeError("OpenAI API response did not contain output text.")

    return text


def call_codex_cli(prompt, evidence, model, timeout):
    codex_bin = os.environ.get("CODEX_BIN", CODEX_BIN)

    if not Path(codex_bin).exists():
        raise RuntimeError(f"Codex CLI not found: {codex_bin}")

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".md",
        delete=False,
        dir=os.environ.get("TMPDIR", "/tmp"),
    ) as output_file:
        output_path = Path(output_file.name)

    codex_prompt = (
        "Generate one complete equity research report in Markdown.\n"
        "Return only the report body. Do not edit files and do not run shell commands.\n"
        "Use only the supplied local evidence plus clearly labelled general industry knowledge.\n"
        "Do not fabricate numbers, quotes, or source references. If evidence is missing, say so.\n\n"
        f"{prompt}\n\n"
        "COMPANY EVIDENCE PACK STARTS BELOW\n\n"
        f"{evidence}\n\n"
        "COMPANY EVIDENCE PACK ENDS"
    )

    cmd = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "-C",
        str(Path.cwd()),
        "-s",
        "workspace-write",
        "-o",
        str(output_path),
        "-",
    ]

    if model:
        cmd.extend(["-m", model])

    try:
        completed = subprocess.run(
            cmd,
            input=codex_prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            raise RuntimeError(
                "Codex CLI failed with exit code "
                f"{completed.returncode}.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )

        report = output_path.read_text(encoding="utf-8").strip()

        if not report:
            raise RuntimeError("Codex CLI produced an empty report.")

        return report
    finally:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass


def write_report(output_path, ticker, model, evidence_chars, report_text):
    header = (
        f"# {ticker} Investment Analysis\n\n"
        f"Generated on: {date.today().isoformat()}\n\n"
        f"Model: {model}\n\n"
        f"Evidence characters supplied: {evidence_chars}\n\n"
        "---\n\n"
    )

    output_path.write_text(header + report_text.strip() + "\n", encoding="utf-8")


def process_ticker(ticker_dir, args, prompt):
    ticker = ticker_dir.name
    output_path = ticker_dir / args.output

    if output_path.exists() and not args.force and not args.dry_run:
        print(f"SKIP {ticker}: {output_path.name} already exists. Use --force to overwrite.")
        return "skipped"

    print(f"\n{'=' * 70}")
    print(f"Processing {ticker}")
    print(f"{'=' * 70}")

    evidence = build_evidence_pack(
        ticker_dir=ticker_dir,
        total_limit=args.evidence_chars,
        per_file_limit=args.per_file_chars,
    )

    print(f"Evidence pack size: {len(evidence):,} chars")

    if args.save_evidence or args.dry_run:
        evidence_path = ticker_dir / "analysis_evidence_pack.md"
        evidence_path.write_text(evidence, encoding="utf-8")
        print(f"Saved evidence pack: {evidence_path}")

    if args.dry_run:
        return "dry-run"

    if args.backend == "openai-api":
        report = call_openai(
            prompt=prompt,
            evidence=evidence,
            model=args.model,
            max_output_tokens=args.max_output_tokens,
            temperature=args.temperature,
        )
    else:
        report = call_codex_cli(
            prompt=prompt,
            evidence=evidence,
            model=args.model,
            timeout=args.codex_timeout,
        )

    write_report(
        output_path=output_path,
        ticker=ticker,
        model=args.model,
        evidence_chars=len(evidence),
        report_text=report,
    )

    print(f"Saved report: {output_path}")
    return "created"


def main():
    parser = argparse.ArgumentParser(
        description="Generate investment analysis markdown reports for ticker folders."
    )
    parser.add_argument("--data-dir", default=str(BASE_DIR))
    parser.add_argument("--prompt-file", default=str(PROMPT_FILE))
    parser.add_argument("--symbols", nargs="*", help="Ticker symbols, comma-separated or space-separated.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--backend",
        choices=["codex-cli", "openai-api"],
        default="codex-cli",
        help="Use Codex desktop login or direct OpenAI API.",
    )
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", ""))
    parser.add_argument("--evidence-chars", type=int, default=220000)
    parser.add_argument("--per-file-chars", type=int, default=25000)
    parser.add_argument("--max-output-tokens", type=int, default=12000)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--codex-timeout", type=int, default=1800, help="Seconds per ticker for codex-cli.")
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds to wait between API calls.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing reports.")
    parser.add_argument("--dry-run", action="store_true", help="Only build evidence packs; do not call the API.")
    parser.add_argument("--save-evidence", action="store_true", help="Save analysis_evidence_pack.md beside each report.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    prompt_file = Path(args.prompt_file)

    if not prompt_file.exists():
        print(f"Prompt file not found: {prompt_file}", file=sys.stderr)
        return 1

    prompt = prompt_file.read_text(encoding="utf-8")
    selected = parse_symbols(args.symbols)
    ticker_dirs = discover_tickers(data_dir)

    if selected:
        ticker_dirs = [path for path in ticker_dirs if path.name.upper() in selected]

    if not ticker_dirs:
        print("No ticker folders matched.")
        return 1

    print(f"Ticker folders selected: {len(ticker_dirs)}")
    print(f"Backend: {args.backend}")
    print(f"Model: {args.model or 'default'}")

    counts = {"created": 0, "skipped": 0, "dry-run": 0, "failed": 0}

    for ticker_dir in ticker_dirs:
        try:
            status = process_ticker(ticker_dir, args, prompt)
            counts[status] += 1
        except Exception as exc:
            counts["failed"] += 1
            print(f"FAILED {ticker_dir.name}: {exc}", file=sys.stderr)

        if not args.dry_run and args.sleep > 0:
            time.sleep(args.sleep)

    print("\nDONE")
    print(json.dumps(counts, indent=2))

    return 0 if counts["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
