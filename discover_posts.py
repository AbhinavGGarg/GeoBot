import argparse
import csv
import random
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from selenium import webdriver
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager


def setup_driver():
    options = ChromeOptions()
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--start-maximized")
    user_data_dir = Path.cwd() / "chrome_data"
    options.add_argument(f"--user-data-dir={user_data_dir}")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def normalize_url(url):
    if not url:
        return ""
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    parts = urlsplit(url)
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return clean


def load_keywords(path):
    return [
        line.strip().lower()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def load_group_urls(path):
    urls = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("group_url") or "").strip()
            if url:
                urls.append(url)
    return urls


def extract_group_posts(driver, keywords, max_per_group):
    found = []
    articles = driver.find_elements(By.XPATH, "//div[@role='article']")
    for article in articles:
        text = (article.text or "").strip()
        if len(text) < 40:
            continue

        lower = text.lower()
        matched = [k for k in keywords if k in lower]
        if not matched:
            continue

        links = article.find_elements(By.TAG_NAME, "a")
        post_url = ""
        for link in links:
            href = normalize_url(link.get_attribute("href") or "")
            if "/posts/" in href or "permalink.php" in href:
                post_url = href
                break

        if not post_url:
            continue

        context = text.replace("\n", " ")[:450]
        found.append({
            "post_url": post_url,
            "context": f"Matched keywords: {', '.join(matched[:5])}. Visible post context: {context}",
            "status": "pending"
        })

        if len(found) >= max_per_group:
            break

    return found


def append_unique_posts(path, rows):
    existing = set()
    out_path = Path(path)

    if out_path.exists():
        with open(out_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing.add((row.get("post_url") or "").strip())

    write_header = not out_path.exists() or out_path.stat().st_size == 0

    added = 0
    with open(out_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["post_url", "context", "status"])
        if write_header:
            writer.writeheader()

        for row in rows:
            if row["post_url"] in existing:
                continue
            writer.writerow(row)
            existing.add(row["post_url"])
            added += 1

    return added


def main():
    parser = argparse.ArgumentParser(description="Discover Facebook group posts matching Geodo keywords")
    parser.add_argument("--groups", default="group_urls.csv")
    parser.add_argument("--keywords", default="keywords.txt")
    parser.add_argument("--output", default="post_urls.csv")
    parser.add_argument("--scrolls", type=int, default=4)
    parser.add_argument("--max-per-group", type=int, default=5)
    parser.add_argument("--delay-min", type=float, default=4)
    parser.add_argument("--delay-max", type=float, default=8)
    args = parser.parse_args()

    keywords = load_keywords(args.keywords)
    group_urls = load_group_urls(args.groups)

    if not keywords:
        raise SystemExit("No keywords found.")
    if not group_urls:
        raise SystemExit("No group URLs found.")

    driver = setup_driver()

    try:
        total_added = 0
        print("Chrome is opening. Make sure you are logged into Facebook.")

        for group_url in group_urls:
            print(f"\nOpening group: {group_url}")
            driver.get(group_url)
            time.sleep(random.uniform(args.delay_min, args.delay_max))

            all_rows = []
            for i in range(args.scrolls):
                print(f"  Scroll {i + 1}/{args.scrolls}")
                driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * 0.9));")
                time.sleep(random.uniform(args.delay_min, args.delay_max))
                rows = extract_group_posts(driver, keywords, args.max_per_group)
                all_rows.extend(rows)

            added = append_unique_posts(args.output, all_rows)
            total_added += added
            print(f"  Added {added} matching post URLs.")

        print(f"\nDone. Added {total_added} new post URLs to {args.output}.")
        input("Press Enter to close Chrome...")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
