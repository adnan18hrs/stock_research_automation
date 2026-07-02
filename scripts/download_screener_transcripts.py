import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup


BASE_DIR = Path("data")
MAX_TRANSCRIPTS = 10
OLD_CONCALL_DIRS = (
    "concalls",
    "concall_transcripts",
    "concall_updates",
)

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-IN,en-US;q=0.9,en-GB;q=0.8,en;q=0.7",
    "cache-control": "max-age=0",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
}


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


def ticker_dirs(base_dir, selected):
    dirs = sorted(path for path in base_dir.iterdir() if path.is_dir())

    if selected:
        dirs = [path for path in dirs if path.name.upper() in selected]

    return dirs


def clear_old_concall_dirs(ticker_dir):
    for folder_name in OLD_CONCALL_DIRS:
        target = ticker_dir / folder_name
        if target.exists():
            shutil.rmtree(target)


def screener_url(symbol):
    return f"https://www.screener.in/company/{symbol}/consolidated/"


def refresh_screener_page(session, ticker_dir, sleep_seconds):
    symbol = ticker_dir.name
    url = screener_url(symbol)
    response = session.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()

    target_dir = ticker_dir / "screener_finance"
    target_dir.mkdir(parents=True, exist_ok=True)

    target_file = target_dir / "company_page.html"
    target_file.write_text(response.text, encoding="utf-8")

    if sleep_seconds > 0:
        time.sleep(sleep_seconds)

    return target_file


def load_screener_html(ticker_dir):
    html_path = ticker_dir / "screener_finance" / "company_page.html"

    if not html_path.exists():
        return None

    return html_path.read_text(encoding="utf-8", errors="ignore")


def safe_name(text):
    text = unquote(text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    return text[:120] or "transcript"


def filename_from_url(url, index, period):
    parsed = urlparse(url)
    filename = Path(parsed.path).name

    if parsed.netloc.endswith("bseindia.com"):
        pname = parse_qs(parsed.query).get("Pname", [""])[0]
        if pname:
            filename = pname

    if not filename.lower().endswith(".pdf"):
        filename = f"transcript_{index:02d}.pdf"

    period_prefix = safe_name(period) if period else f"transcript_{index:02d}"
    return f"{index:02d}_{period_prefix}_{safe_name(filename)}"


def extract_transcripts(html, max_transcripts):
    soup = BeautifulSoup(html, "html.parser")
    transcripts = []
    seen = set()

    for link in soup.select("a.concall-link"):
        title = (link.get("title") or "").strip().lower()
        text = link.get_text(" ", strip=True).strip().lower()
        href = link.get("href")

        if not href:
            continue

        is_transcript = title == "raw transcript" or text == "transcript"
        is_pdf = ".pdf" in href.lower() or "annpdfopen.aspx" in href.lower()

        if not is_transcript or not is_pdf:
            continue

        if href in seen:
            continue

        seen.add(href)

        period = ""
        row = link.find_parent("li")
        if row:
            date_node = row.select_one(".ink-600")
            if date_node:
                period = date_node.get_text(" ", strip=True)

        transcripts.append(
            {
                "period": period,
                "url": href,
            }
        )

        if len(transcripts) >= max_transcripts:
            break

    return transcripts


def download_pdf(session, url, output_path):
    headers = dict(HEADERS)
    headers["accept"] = "application/pdf,text/html,*/*"
    headers["referer"] = "https://www.screener.in/"

    try:
        response = session.get(url, headers=headers, timeout=90, allow_redirects=True)
        response.raise_for_status()

        content = response.content

        if len(content) < 1000:
            raise RuntimeError("Downloaded file is unexpectedly small.")

        output_path.write_bytes(content)
        return
    except Exception as requests_error:
        curl_cmd = [
            "curl",
            "-L",
            "--fail",
            "--retry",
            "2",
            "--connect-timeout",
            "30",
            "--max-time",
            "180",
            "-A",
            HEADERS["user-agent"],
            "-e",
            "https://www.screener.in/",
            "-H",
            "accept: application/pdf,text/html,*/*",
            "-o",
            str(output_path),
            url,
        ]

        completed = subprocess.run(
            curl_cmd,
            text=True,
            capture_output=True,
            check=False,
        )

        if completed.returncode != 0:
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(
                f"{requests_error}; curl fallback failed: {completed.stderr.strip()}"
            )

        if output_path.stat().st_size < 1000:
            output_path.unlink()
            raise RuntimeError(
                f"{requests_error}; curl fallback produced an unexpectedly small file."
            )


def save_metadata(ticker_dir, transcripts):
    metadata_path = ticker_dir / "concalls" / "screener_transcripts.json"
    metadata_path.write_text(
        json.dumps(transcripts, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def process_ticker(session, ticker_dir, args):
    symbol = ticker_dir.name

    if args.clear_old:
        clear_old_concall_dirs(ticker_dir)

    if args.refresh_pages:
        refresh_screener_page(session, ticker_dir, args.sleep)

    html = load_screener_html(ticker_dir)
    if not html:
        print(f"{symbol}: no screener HTML found")
        return {"symbol": symbol, "found": 0, "downloaded": 0, "failed": 0}

    transcripts = extract_transcripts(html, args.max)
    target_dir = ticker_dir / "concalls"
    target_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    failed = 0
    metadata = []

    for index, item in enumerate(transcripts, start=1):
        filename = filename_from_url(item["url"], index, item["period"])
        output_path = target_dir / filename

        row = {
            "period": item["period"],
            "url": item["url"],
            "file": str(output_path.relative_to(ticker_dir)),
        }

        if args.dry_run:
            metadata.append(row)
            continue

        try:
            download_pdf(session, item["url"], output_path)
            downloaded += 1
            metadata.append(row)
        except Exception as exc:
            failed += 1
            row["error"] = str(exc)
            metadata.append(row)
            print(f"{symbol}: failed {item['period'] or index}: {exc}")

        if args.sleep > 0:
            time.sleep(args.sleep)

    save_metadata(ticker_dir, metadata)
    print(f"{symbol}: found={len(transcripts)} downloaded={downloaded} failed={failed}")

    return {
        "symbol": symbol,
        "found": len(transcripts),
        "downloaded": downloaded,
        "failed": failed,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Download latest Screener raw transcript PDFs into data/<ticker>/concalls."
    )
    parser.add_argument("--data-dir", default=str(BASE_DIR))
    parser.add_argument("--symbols", nargs="*", help="Ticker symbols, comma-separated or space-separated.")
    parser.add_argument("--max", type=int, default=MAX_TRANSCRIPTS, help="Max transcripts per ticker.")
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--refresh-pages", action="store_true", help="Re-download Screener company pages before parsing.")
    parser.add_argument("--clear-old", action="store_true", help="Delete old concalls/concall_transcripts/concall_updates folders first.")
    parser.add_argument("--dry-run", action="store_true", help="Parse links and write metadata without downloading PDFs.")
    args = parser.parse_args()

    selected = parse_symbols(args.symbols)
    dirs = ticker_dirs(Path(args.data_dir), selected)

    if not dirs:
        print("No ticker folders matched.")
        return 1

    session = requests.Session()

    total = {
        "tickers": 0,
        "found": 0,
        "downloaded": 0,
        "failed": 0,
    }

    for ticker_dir in dirs:
        result = process_ticker(session, ticker_dir, args)
        total["tickers"] += 1
        total["found"] += result["found"]
        total["downloaded"] += result["downloaded"]
        total["failed"] += result["failed"]

    print("\nDONE")
    print(json.dumps(total, indent=2))
    return 0 if total["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
