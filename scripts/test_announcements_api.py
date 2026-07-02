import requests

symbol = "ITC"
issuer = "ITC Limited"

session = requests.Session()

headers = {
    "User-Agent": "Mozilla/5.0"
}

session.get(
    "https://www.nseindia.com",
    headers=headers
)

url = (
    "https://www.nseindia.com/api/"
    "corporate-announcements"
)

params = {
    "index": "equities",
    "symbol": symbol,
    "reqXbrl": "false",
    "issuer": issuer
}

r = session.get(
    url,
    headers=headers,
    params=params
)

print(r.status_code)

data = r.json()

print(
    f"Records: {len(data)}"
)

for row in data[:10]:

    print(
        row["desc"]
    )

    print(
        row.get(
            "attchmntFile"
        )
    )

    print("-" * 50)
