import argparse
import csv
import random
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

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


def extract_post_url_from_article(article):
    links = article.find_elements(By.XPATH, ".//a[@href]")
    for link in links:
        href = normalize_url(link.get_attribute("href") or "")
        if not href:
            continue
        if "facebook.com/groups/" in href and "/posts/" in href:
            return href
        if "facebook.com/permalink.php" in href:
            return href
    return ""


def article_matches(text: str, keywords):
    lower = text.lower()
    matched = [kw for kw in keywords if kw in lower]

    # Extra broad signals so obvious posts like GTM/traction/first users do not get missed.
    broad = [
        "gtm", "go to market", "marketing", "traction", "first 100 users",
        "users", "conversion", "sales", "leads", "lead", "pipeline",
        "crm", "saas", "b2b", "founder", "startup", "pricing",
        "landing page", "outbound", "cold email", "growth"
    ]

    for signal in broad:
        if signal in lower and signal not in matched:
            matched.append(signal)

    return matched


def find_comment_box_inside_article(article):
    # First try visible comment boxes already inside the post.
    selectors = [
        'div[contenteditable="true"][aria-label*="Comment"]',
        'div[contenteditable="true"][aria-label*="Answer"]',
        'div[contenteditable="true"][aria-label*="Write"]',
        'div[role="textbox"][contenteditable="true"]',
    ]

    for css in selectors:
        try:
            boxes = article.find_elements(By.CSS_SELECTOR, css)
            for box in boxes:
                if box.is_displayed():
                    return box
        except Exception:
            pass

    # If the box is not open, click the Comment button inside the article.
    click_targets = [
        ".//*[normalize-space()='Comment']",
        ".//*[contains(@aria-label, 'Comment')]",
        ".//*[contains(text(), 'Comment')]",
    ]

    for xpath in click_targets:
        try:
            els = article.find_elements(By.XPATH, xpath)
            for el in els:
                try:
                    if el.is_displayed():
                        el.click()
                        time.sleep(1)
                        break
                except Exception:
                    continue
        except Exception:
            pass

    # Try again after clicking Comment.
    for css in selectors:
        try:
            boxes = article.find_elements(By.CSS_SELECTOR, css)
            for box in boxes:
                if box.is_displayed():
                    return box
        except Exception:
            pass

    return None


def type_comment_inline(driver, article, comment: str) -> bool:
    box = find_comment_box_inside_article(article)
    if not box:
        return False

    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
    time.sleep(0.8)
    box.click()
    time.sleep(0.8)

    subprocess.run("pbcopy", input=comment, text=True, check=True)

    ActionChains(driver) \
        .key_down(Keys.COMMAND) \
        .send_keys("v") \
        .key_up(Keys.COMMAND) \
        .perform()

    time.sleep(1)
    return True


def process_article_inline(driver, article, context, fake_url):
    visible_text = (article.text or "").strip()
    comment = generate_openai_comment(context, visible_text)

    if comment.strip().upper() == "SKIP":
        print("    AI/template skipped this post.")
        write_log(LeadRow(fake_url, context, "pending"), comment, "skipped")
        return False

    print("\nGenerated comment:")
    print(comment)
    print("\nTyping draft directly into visible group post...")

    ok = type_comment_inline(driver, article, comment)

    if ok:
        print("Draft typed inline. Page/tab stays open.")
        write_log(LeadRow(fake_url, context, "pending"), comment, "typed_inline_draft")
        return True

    print("Could not find inline comment box.")
    write_log(LeadRow(fake_url, context, "pending"), comment, "inline_comment_box_not_found")
    return False


def process_post_in_new_tab(driver, post_url, context, scan_tab, args):
    print(f"\nOpening matching post in new tab: {post_url}")

    driver.switch_to.new_window("tab")
    driver.get(post_url)

    time.sleep(random.uniform(args.post_load_min, args.post_load_max))

    visible_text = extract_visible_post_text(driver)
    comment = generate_openai_comment(context, visible_text)

    if comment.strip().upper() == "SKIP":
        print("AI/template skipped this post.")
        write_log(LeadRow(post_url, context, "pending"), comment, "skipped")
        driver.switch_to.window(scan_tab)
        return False

    print("\nGenerated comment:")
    print(comment)
    print("\nTyping draft into new-tab post...")

    type_comment(driver, comment)

    print("Draft typed. Tab stays open.")
    write_log(LeadRow(post_url, context, "pending"), comment, "typed_new_tab_draft")

    driver.switch_to.window(scan_tab)
    return True


def run_live(args):
    load_dotenv()

    keywords = load_keywords(args.keywords)
    group_urls = load_group_urls(args.groups)

    if not group_urls:
        raise SystemExit("No groups found in group_urls.csv")
    if not keywords:
        raise SystemExit("No keywords found in keywords.txt")

    driver = setup_driver()
    seen = set()
    drafted = 0

    print("\nChrome is opening. Make sure you are logged into Facebook.")
    print("This runs until Control+C.")
    print("Flow: scan groups → find relevant article → open post in new tab if possible OR draft inline → keep Chrome open.\n")

    try:
        scan_tab = driver.current_window_handle
        cycle = 0

        while True:
            cycle += 1
            print(f"\n========== Scan cycle {cycle} ==========")

            for group_url in group_urls:
                driver.switch_to.window(scan_tab)
                print(f"\nScanning group: {group_url}")
                driver.get(group_url)
                time.sleep(random.uniform(args.group_load_min, args.group_load_max))

                empty_scrolls = 0

                for scroll_num in range(1, args.max_scrolls_per_group + 1):
                    print(f"  Scroll {scroll_num}/{args.max_scrolls_per_group}")

                    articles = driver.find_elements(By.XPATH, "//div[@role='article']")
                    found_on_this_scroll = False

                    for article in articles:
                        text = (article.text or "").strip()
                        if len(text) < 40:
                            continue

                        matched = article_matches(text, keywords)
                        if not matched:
                            continue

                        post_url = extract_post_url_from_article(article)
                        unique_key = post_url if post_url else text[:250]

                        if unique_key in seen:
                            continue

                        seen.add(unique_key)
                        found_on_this_scroll = True

                        context = (
                            f"Matched keywords/signals: {', '.join(matched[:8])}. "
                            f"Visible post context: {text.replace(chr(10), ' ')[:900]}"
                        )

                        print("\nRelevant post found.")
                        print("Matched:", ", ".join(matched[:8]))

                        success = False

                        # Prefer new tab if we can get a real post URL.
                        if args.open_tabs and post_url:
                            success = process_post_in_new_tab(driver, post_url, context, scan_tab, args)
                        else:
                            success = process_article_inline(driver, article, context, post_url or "inline_feed_post")

                        if success:
                            drafted += 1
                            print(f"Draft count: {drafted}")

                            if drafted >= args.max_drafts:
                                print(f"\nReached max drafts: {args.max_drafts}")
                                print("Chrome is staying open for review.")
                                return

                            cooldown = random.uniform(args.cooldown_min, args.cooldown_max)
                            print(f"Cooldown: waiting {round(cooldown)} seconds...")
                            time.sleep(cooldown)

                        driver.switch_to.window(scan_tab)

                    if found_on_this_scroll:
                        empty_scrolls = 0
                    else:
                        empty_scrolls += 1

                    driver.switch_to.window(scan_tab)
                    driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * 0.95));")
                    time.sleep(random.uniform(args.scroll_delay_min, args.scroll_delay_max))

                    if empty_scrolls >= args.empty_scroll_limit:
                        print(f"  No relevant matches after {empty_scrolls} empty scrolls. Moving to next group.")
                        break

            wait = random.uniform(args.cycle_wait_min, args.cycle_wait_max)
            print(f"\nFinished cycle {cycle}. Drafted so far: {drafted}.")
            print(f"Waiting {round(wait)} seconds, then scanning again...")
            time.sleep(wait)

    except KeyboardInterrupt:
        print("\nStopped with Control+C.")
        print("Chrome is staying open. Review any drafted comments/tabs manually.")

    finally:
        if args.close_when_done:
            driver.quit()
        else:
            print("Leaving Chrome open. Close Chrome manually when done.")


def parse_args():
    parser = argparse.ArgumentParser(description="Geodo live Facebook group scanner and draft commenter")
    parser.add_argument("--groups", default="group_urls.csv")
    parser.add_argument("--keywords", default="keywords.txt")

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

    parser.add_argument("--open-tabs", action="store_true")
    parser.add_argument("--close-when-done", action="store_true")

    return parser.parse_args()


if __name__ == "__main__":
    run_live(parse_args())
