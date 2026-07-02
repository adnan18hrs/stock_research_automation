import json
import requests
from pathlib import Path
from time import sleep

BASE_DIR = Path("data")

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

KEYWORDS = {
    "Transcript": "concall_transcripts",
    "Investor Presentation": "investor_presentations",
    "Link of Recording": "recordings",
    "Analysts/Institutional Investor Meet/Con. Call Updates": "concall_updates"
}

session = requests.Session()

session.get(
    "https://www.nseindia.com",
    headers=HEADERS,
    timeout=30
)

def download_file(url, filepath):

    try:

        r = session.get(
            url,
            headers=HEADERS,
            timeout=60
        )

        r.raise_for_status()

        with open(filepath, "wb") as f:
            f.write(r.content)

        print(
            f"Downloaded: {filepath.name}"
        )

    except Exception as e:

        print(
            f"Failed: {url}"
        )

        print(e)

for company_dir in BASE_DIR.iterdir():

    if not company_dir.is_dir():
        continue

    symbol = company_dir.name

    print(f"\n{'='*60}")
    print(f"Processing {symbol}")
    print(f"{'='*60}")

    announcements_dir = (
        company_dir
        / "announcements"
    )

    announcements_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    try:

        url = (
            "https://www.nseindia.com/api/"
            "corporate-announcements"
        )

        params = {
            "index": "equities",
            "symbol": symbol,
            "reqXbrl": "false"
        }

        r = session.get(
            url,
            headers=HEADERS,
            params=params,
            timeout=60
        )

        data = r.json()

        with open(
            announcements_dir / "raw.json",
            "w"
        ) as f:

            json.dump(
                data,
                f,
                indent=2
            )

        print(
            f"Records: {len(data)}"
        )

        for row in data:

            desc = str(
                row.get("desc", "")
            )

            pdf_url = row.get(
                "attchmntFile"
            )

            if not pdf_url:
                continue

            target_folder = None

            for keyword, folder in KEYWORDS.items():

                if keyword.lower() in desc.lower():

                    target_folder = (
                        company_dir
                        / folder
                    )

                    break

            if not target_folder:
                continue

            target_folder.mkdir(
                parents=True,
                exist_ok=True
            )

            filename = (
                pdf_url.split("/")[-1]
            )

            download_file(
                pdf_url,
                target_folder / filename
            )

        sleep(1)

    except Exception as e:

        print(
            f"Failed symbol {symbol}"
        )

        print(e)

print("\nDONE")
