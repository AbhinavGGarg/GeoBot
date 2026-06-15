import argparse
import csv
import random
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from selenium.webdriver.common.by import By

from batch_runner import (
    LeadRow,
    setup_driver,
    extract_visible_post_text,
    generate_openai_comment,
    type_comment,
    write_log,
)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def load_keywords(path: str):
    return [
        line.strip().lower()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def load_group_urls(path: str):
    urls = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("group_url") or "").strip()
            if url:
                urls.append(url)
    return urls


def load_seen_urls(path: str):
    seen = set()
    p = Path(path)
    if not p.exists():
        return seen

    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("post_url") or "").strip()
            if url:
                seen.add(url)

    return seen


def append_to_post_queue(path: str, post_url: str, context: str):
    p = Path(path)
    has_header = p.exists() and p.stat().st_size > 0

    with open(p, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["post_url", "context", "status"])
        if not has_header:
            writer.writeheader()

        writer.writerow({
            "post_url": post_url,
            "context": context,
            "status": "pending",
        })


def extract_post_url_from_article(article):
    links = article.find_elements(By.XPATH, ".//a[@href]")

    for link in links:
        href = normalize_url(link.get_attribute("href") or "")

        if "facebook.com/groups/" in href and "/posts/" in href:
            return href

        if "facebook.com/permalink.php" in href:
            return href

    return ""


def article_matches(text: str, keywords):
    lower = text.lower()

    matched = [kw for kw in keywords if kw in lower]

    # Broader fallback so it does not miss relevant posts.
    broad_signals = [
        "lead", "sales", "pipeline", "crm", "saas", "startup",
        "founder", "customer", "outreach", "email", "pricing",
        "marketing", "growth", "b2b"
    ]

    for signal in broad_signals:
        if signal in lower and signal not in matched:
            matched.append(signal)

    return matched


def find_matching_articles(driver, keywords, seen_urls):
    matches = []
    articles = driver.find_elements(By.XPATH, "//div[@role='article']")

    for article in articles:
        text = (article.text or "").strip()
        if len(text) < 35:
            continue

        matched = article_matches(text, keywords)
        if not matched:
            continue

        post_url = extract_post_url_from_article(article)
        if not post_url:
            continue

        if post_url in seen_urls:
            continue

        context = (
            f"Matched keywords/signals: {', '.join(matched[:8])}. "
            f"Visible post context: {text.replace(chr(10), ' ')[:700]}"
        )

        matches.append((post_url, context))

    return matches


def open_post_new_tab_and_type(driver, post_url, context, args):
    print(f"\nFound match. Opening post in new tab:")
    print(post_url)

    driver.switch_to.new_window("tab")
    driver.get(post_url)

    time.sleep(random.uniform(args.post_load_min, args.post_load_max))

    visible_text = extract_visible_post_text(driver)
    comment = generate_openai_comment(context, visible_text)

    if comment.strip().upper() == "SKIP":
        print("Skipped: not relevant enough.")
        write_log(LeadRow(post_url, context, "pending"), comment, "skipped")
        return False

    print("\nGenerated comment:")
    print(comment)
    print("\nTyping draft into Facebook...")

    type_comment(driver, comment)

    print("Draft typed. Tab stays open.")
    write_log(LeadRow(post_url, context, "pending"), comment, "typed_left_as_draft")

    return True


def run_live(args):
    load_dotenv()

    keywords = load_keywords(args.keywords)
    group_urls = load_group_urls(args.groups)
    seen_urls = load_seen_urls(args.output)

    if not group_urls:
        raise SystemExit("No groups found in group_urls.csv")
    if not keywords:
        raise SystemExit("No keywords found in keywords.txt")

    driver = setup_driver()
    drafted = 0

    print("\nChrome is opening. Make sure you are logged into Facebook.")
    print("Live mode is running until Control+C.")
    print("Flow: scan groups → scroll until match → open post in new tab → type draft → keep tab open → keep scanning.\n")

    try:
        scan_tab = driver.current_window_handle
        cycle = 0

        while True:
            cycle += 1
            print(f"\n========== Scan cycle {cycle} ==========")

            for group_url in group_urls:
                print(f"\nScanning group: {group_url}")

                driver.switch_to.window(scan_tab)
                driver.get(group_url)
                time.sleep(random.uniform(args.group_load_min, args.group_load_max))

                empty_scrolls = 0

                for scroll_num in range(1, args.max_scrolls_per_group + 1):
                    print(f"  Scroll {scroll_num}/{args.max_scrolls_per_group}")

                    matches = find_matching_articles(driver, keywords, seen_urls)

                    if matches:
                        empty_scrolls = 0

                        for post_url, context in matches:
                            if drafted >= args.max_drafts:
                                print(f"\nReached max drafts: {args.max_drafts}")
                                print("Leaving Chrome open for review.")
                                return

                            seen_urls.add(post_url)
                            append_to_post_queue(args.output, post_url, context)

                            success = open_post_new_tab_and_type(driver, post_url, context, args)

                            driver.switch_to.window(scan_tab)

                            if success:
                                drafted += 1
                                cooldown = random.uniform(args.cooldown_min, args.cooldown_max)
                                print(f"Cooldown: waiting {round(cooldown)} seconds before continuing...")
                                time.sleep(cooldown)

                    else:
                        empty_scrolls += 1

                    driver.switch_to.window(scan_tab)
                    driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * 0.95));")
                    time.sleep(random.uniform(args.scroll_delay_min, args.scroll_delay_max))

                    if empty_scrolls >= args.empty_scroll_limit:
                        print(f"  No matches after {empty_scrolls} scrolls. Moving to next group.")
                        break

            print("\nFinished all groups this cycle.")
            print(f"Drafted so far: {drafted}")
            wait = random.uniform(args.cycle_wait_min, args.cycle_wait_max)
            print(f"Waiting {round(wait)} seconds, then scanning again...")
            time.sleep(wait)

    except KeyboardInterrupt:
        print("\nStopped by Control+C.")
        print("Chrome is staying open. Review drafted tabs manually.")

    finally:
        if args.close_when_done:
            driver.quit()
        else:
            print("Leaving Chrome open. Close it manually when done.")


def parse_args():
    parser = argparse.ArgumentParser(description="Geodo live Facebook scanner and draft commenter")
    parser.add_argument("--groups", default="group_urls.csv")
    parser.add_argument("--keywords", default="keywords.txt")
    parser.add_argument("--output", default="post_urls.csv")

    parser.add_argument("--max-drafts", type=int, default=5)
    parser.add_argument("--max-scrolls-per-group", type=int, default=80)
    parser.add_argument("--empty-scroll-limit", type=int, default=15)

    parser.add_argument("--group-load-min", type=float, default=4)
    parser.add_argument("--group-load-max", type=float, default=7)
    parser.add_argument("--post-load-min", type=float, default=4)
    parser.add_argument("--post-load-max", type=float, default=7)
    parser.add_argument("--scroll-delay-min", type=float, default=2)
    parser.add_argument("--scroll-delay-max", type=float, default=4)

    parser.add_argument("--cooldown-min", type=float, default=120)
    parser.add_argument("--cooldown-max", type=float, default=180)

    parser.add_argument("--cycle-wait-min", type=float, default=60)
    parser.add_argument("--cycle-wait-max", type=float, default=120)

    parser.add_argument("--close-when-done", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    run_live(parse_args())
