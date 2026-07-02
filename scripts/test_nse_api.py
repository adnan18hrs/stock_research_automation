import requests

session = requests.Session()

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        " AppleWebKit/537.36 (KHTML, like Gecko)"
        " Chrome/125.0 Safari/537.36"
    )
}

# NSE ko pehle visit karna padta hai
session.get(
    "https://www.nseindia.com",
    headers=headers,
    timeout=30
)

url = (
    "https://www.nseindia.com/api/annual-reports"
    "?index=equities&symbol=KARURVYSYA"
)

response = session.get(
    url,
    headers=headers,
    timeout=30
)

print(response.status_code)
print(response.text[:500])
