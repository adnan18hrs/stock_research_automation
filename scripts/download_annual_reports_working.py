import pandas as pd
import requests
from pathlib import Path
from time import sleep
print("SCRIPT STARTED")
# -----------------------------------
# CONFIG
# -----------------------------------

MAX_REPORTS = 5

CSV_FILE = "config/nifty100.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X)"
        " AppleWebKit/537.36"
        " Chrome/125 Safari/537.36"
    )
}

# -----------------------------------
# NSE SESSION
# -----------------------------------

session = requests.Session()

session.get(
    "https://www.nseindia.com",
    headers=HEADERS,
    timeout=30
)

# -----------------------------------
# DOWNLOAD FUNCTION
# -----------------------------------

def download_reports(symbol):

    print(f"\n{'='*60}")
    print(f"Processing: {symbol}")
    print(f"{'='*60}")

    try:

        api_url = (
            "https://www.nseindia.com/api/"
            f"annual-reports?index=equities&symbol={symbol}"
        )

        response = session.get(
            api_url,
            headers=HEADERS,
            timeout=30
        )

        response.raise_for_status()

        data = response.json().get("data", [])

        if not data:
            print("No reports found")
            return

        reports = data[:MAX_REPORTS]

        base_dir = (
            Path("data")
            / symbol
            / "annual_reports"
        )

        base_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        print(
            f"Found {len(reports)} reports"
        )

        for report in reports:

            pdf_url = report["fileName"]

            from_year = report["fromYr"]
            to_year = report["toYr"]

            filename = (
                f"{from_year}_{to_year}.pdf"
            )

            filepath = (
                base_dir / filename
            )

            print(
                f"Downloading {filename}"
            )

            pdf = session.get(
                pdf_url,
                headers=HEADERS,
                timeout=60
            )

            with open(filepath, "wb") as f:
                f.write(pdf.content)

        print("Completed")

        sleep(1)

    except Exception as e:

        print(
            f"Failed: {symbol}"
        )

        print(str(e))

# -----------------------------------
# MAIN
# -----------------------------------

if __name__ == "__main__":

    df = pd.read_csv(CSV_FILE)

    if "SYMBOL" not in df.columns:
        raise Exception(
            "CSV must contain SYMBOL column"
        )

    symbols = (
        df["SYMBOL"]
        .dropna()
        .astype(str)
        .unique()
    )

    print(
        f"\nTotal Symbols: {len(symbols)}\n"
    )

    for symbol in symbols:

        download_reports(
            symbol.strip().upper()
        )

    print(
        "\nAll downloads completed."
    )
