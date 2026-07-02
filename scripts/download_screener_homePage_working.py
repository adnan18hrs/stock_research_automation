import requests
from pathlib import Path
from time import sleep

BASE_DIR = Path("data")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Macintosh; Intel Mac OS X 10_15_7)"
        " AppleWebKit/537.36 "
        "(KHTML, like Gecko)"
        " Chrome/125 Safari/537.36"
    )
}

session = requests.Session()


def download_screener_page(symbol):

    url = (
        f"https://www.screener.in/company/"
        f"{symbol}/consolidated/"
    )

    r = session.get(
        url,
        headers=HEADERS,
        timeout=60
    )

    if r.status_code != 200:

        print(
            f"Failed {symbol}"
        )

        return

    target_dir = (
        BASE_DIR
        / symbol
        / "screener_finance"
    )

    target_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    target_file = (
        target_dir
        / "company_page.html"
    )

    target_file.write_text(
        r.text,
        encoding="utf-8"
    )

    print(
        f"Saved: {symbol}"
    )


for company_dir in BASE_DIR.iterdir():

    if not company_dir.is_dir():
        continue

    symbol = company_dir.name.strip()

    try:

        download_screener_page(
            symbol
        )

        sleep(1)

    except Exception as e:

        print(
            f"Error: {symbol}"
        )

        print(e)

print("\nDONE")
