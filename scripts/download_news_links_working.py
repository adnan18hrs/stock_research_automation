import argparse
import csv
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote_plus

import requests
from ddgs import DDGS


BASE_DIR = Path("data")
CSV_FILE = Path("config/nifty100.csv")
MAX_LINKS = 15

GOOGLE_NEWS_URL = (
    "https://news.google.com/rss/search"
    "?q={query}"
    "&hl=en-IN"
    "&gl=IN"
    "&ceid=IN:en"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def clean_company_name(name):
    name = re.sub(r"\bLtd\.?$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\bLimited$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name)
    return name.strip(" ,.-")


def load_company_names(csv_file):
    names = {}

    if not csv_file.exists():
        return names

    with csv_file.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)

        for row in reader:
            symbol = (row.get("SYMBOL") or "").strip().upper()
            company = (row.get("Company Name") or "").strip()

            if symbol and company:
                names[symbol] = clean_company_name(company)

    return names


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


def build_query(symbol, company_name):
    if company_name:
        return f'"{company_name}" {symbol} stock OR shares'
    return f'"{symbol}" stock shares India'


def normalize_url(url):
    return (url or "").strip()


def google_news_search(query, max_links, timeout=60):
    url = GOOGLE_NEWS_URL.format(query=quote_plus(query))
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    items = []
    seen = set()

    for item in root.findall("./channel/item"):
        link = normalize_url(item.findtext("link"))

        if not link or link in seen:
            continue

        seen.add(link)

        source = item.find("source")
        source_name = source.text.strip() if source is not None and source.text else ""

        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "link": link,
                "source": source_name,
                "published": (item.findtext("pubDate") or "").strip(),
                "provider": "google_news",
            }
        )

        if len(items) >= max_links:
            break

    return items


def ddgs_news_fallback(query, max_links):
    items = []
    seen = set()

    try:
        with DDGS() as ddgs:
            results = ddgs.news(query, max_results=max_links * 3)

            for row in results:
                link = normalize_url(row.get("url") or row.get("href"))

                if not link or link in seen:
                    continue

                seen.add(link)

                items.append(
                    {
                        "title": (row.get("title") or "").strip(),
                        "link": link,
                        "source": (row.get("source") or "").strip(),
                        "published": (row.get("date") or row.get("published") or "").strip(),
                        "provider": "ddgs_news_fallback",
                    }
                )

                if len(items) >= max_links:
                    break

    except Exception as exc:
        print(f"Fallback search error: {exc}")

    return items


def get_news_items(query, max_links, timeout=60, use_fallback=True):
    try:
        items = google_news_search(query, max_links, timeout=timeout)

        if items:
            return items

        print("Google News returned 0 results; trying fallback news search.")
    except Exception as exc:
        print(f"Google News search error: {exc}")
        print("Trying fallback news search.")

    if not use_fallback:
        return []

    return ddgs_news_fallback(query, max_links)


def write_outputs(news_dir, items):
    news_dir.mkdir(parents=True, exist_ok=True)

    links_file = news_dir / "news_links.txt"
    metadata_file = news_dir / "news_links.json"

    links_file.write_text(
        "".join(f"{item['link']}\n" for item in items),
        encoding="utf-8",
    )

    metadata_file.write_text(
        json.dumps(items, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def iter_company_dirs(base_dir, selected):
    dirs = sorted(path for path in base_dir.iterdir() if path.is_dir())

    if selected:
        dirs = [path for path in dirs if path.name.upper() in selected]

    return dirs


def main():
    parser = argparse.ArgumentParser(
        description="Download Google News vertical links for each ticker."
    )
    parser.add_argument("--data-dir", default=str(BASE_DIR))
    parser.add_argument("--csv-file", default=str(CSV_FILE))
    parser.add_argument("--symbols", nargs="*", help="Ticker symbols, comma-separated or space-separated.")
    parser.add_argument("--max-links", type=int, default=MAX_LINKS)
    parser.add_argument("--sleep", type=float, default=1.0)
    args = parser.parse_args()

    base_dir = Path(args.data_dir)
    selected = parse_symbols(args.symbols)
    company_names = load_company_names(Path(args.csv_file))
    company_dirs = iter_company_dirs(base_dir, selected)

    if not company_dirs:
        print("No ticker folders matched.")
        return 1

    total_links = 0

    for company_dir in company_dirs:
        symbol = company_dir.name.strip().upper()
        company_name = company_names.get(symbol, "")
        query = build_query(symbol, company_name)

        print("\n" + "=" * 60)
        print(f"Processing {symbol}")
        print("=" * 60)
        print(f"Google News query: {query}")

        items = get_news_items(query, args.max_links)
        write_outputs(company_dir / "news", items)

        total_links += len(items)
        print(f"Saved {len(items)} news links")

        if args.sleep > 0:
            time.sleep(args.sleep)

    print("\nDONE")
    print(f"Tickers processed: {len(company_dirs)}")
    print(f"Total links saved: {total_links}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
