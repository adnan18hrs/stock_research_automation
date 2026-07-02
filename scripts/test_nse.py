from playwright.sync_api import sync_playwright

with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=False
    )

    page = browser.new_page()

    page.goto(
        "https://www.nseindia.com",
        wait_until="networkidle"
    )

    print(page.title())

    input("Press Enter...")

    browser.close()
