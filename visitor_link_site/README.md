# Visitor Link Site

Ye ek simple consent-based product link site hai. Visitor jab analytics allow karta hai, backend visit/click details log karta hai aur Gmail par instant email alert bhejta hai.
Visitor agar email field me apna email share karta hai, to woh email alert me include hota hai.

## Important privacy limits

- Instagram username, name, phone, email automatically nahi milte.
- Browser kisi dusri website, Chrome profile, ya extension storage se Gmail read nahi karne deta.
- Email tabhi milega jab visitor khud form me dale ya proper Google OAuth/Login permission de.
- Exact GPS location sirf tab milti hai jab visitor browser permission allow kare.
- IP se approximate country/state/city mil sakti hai, lekin exact address nahi.
- Visitor ko clear privacy notice/consent dikhana zaroori hai.

## Setup

1. `.env.example` ko `.env` me copy karo.
2. Gmail App Password banao: <https://myaccount.google.com/apppasswords>
3. `.env` me `SMTP_USERNAME`, `SMTP_PASSWORD`, `FROM_EMAIL`, aur `ALERT_TO_EMAIL` set karo.
4. Optional approximate IP location ke liye `IPINFO_TOKEN` set karo.

## Run

```bash
cd visitor_link_site
python3 server.py
```

Open:

```text
http://127.0.0.1:8080
```

Events local file me bhi save hote hain:

```text
visitor_link_site/data/events.jsonl
```

## Product links edit karna

Product cards yahan edit karo:

```text
visitor_link_site/public/index.html
```

Button ke `data-url` me Amazon/Flipkart affiliate ya normal product URL daal sakte ho.
