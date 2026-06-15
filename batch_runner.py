import argparse
import csv
import os
import random
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


GEODO_CONTEXT = """
Geodo is an AI-powered go-to-market platform for B2B sales teams.
It helps with lead generation, outreach, pipeline management, chatbots, and deal coaching.
Comments should be short, helpful, specific, and not spammy.
Mention Geodo only when relevant.
Use https://www.geodo.ai/ only when the post is clearly asking for tools, sales help, GTM help, or recommendations.
"""

DEFAULT_TEMPLATES = [
    "This is a common GTM problem — finding leads is only one piece. The harder part is keeping outreach and pipeline follow-up consistent. Geodo is built around that AI-assisted workflow for B2B sales teams.",
    "A lot of B2B teams hit this once outbound starts scaling. Lead gen, follow-up, and deal coaching get disconnected fast, which is the workflow Geodo is focused on helping sales teams clean up.",
    "If you’re looking at AI for sales workflows, Geodo might be relevant here. It helps B2B teams across lead generation, outreach, pipeline management, chatbots, and deal coaching: https://www.geodo.ai/",
    "The hard part usually is not just getting more leads — it’s making sure the team follows up correctly and keeps the pipeline moving. Geodo is built for that GTM process.",
    "This is exactly why GTM automation is getting interesting. Tools like Geodo are trying to make lead gen, outreach, and pipeline follow-up feel less manual for B2B sales teams.",
]


@dataclass
class LeadRow:
    post_url: str
    context: str
    status: str


def setup_driver() -> webdriver.Chrome:
    options = ChromeOptions()
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--start-maximized")
    options.add_experimental_option("detach", True)
    user_data_dir = Path.cwd() / "chrome_data"
    options.add_argument(f"--user-data-dir={user_data_dir}")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def read_csv(path: str) -> List[LeadRow]:
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            post_url = (raw.get("post_url") or "").strip()
            context = (raw.get("context") or "").strip()
            status = (raw.get("status") or "pending").strip().lower()
            if post_url:
                rows.append(LeadRow(post_url=post_url, context=context, status=status))
    return rows


def write_log(row: LeadRow, comment: str, result: str) -> None:
    log_path = Path("logs") / "comments_log.csv"
    log_path.parent.mkdir(exist_ok=True)
    exists = log_path.exists()

    with open(log_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp", "post_url", "context", "comment", "result"],
        )
        if not exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "post_url": row.post_url,
            "context": row.context,
            "comment": comment,
            "result": result,
        })


def expand_visible_comments(driver, max_clicks: int = 3) -> None:
    xpaths = [
        "//*[contains(text(), 'View') and contains(text(), 'comments')]",
        "//*[contains(text(), 'View') and contains(text(), 'replies')]",
        "//*[contains(text(), 'View') and contains(text(), 'more')]",
        "//*[contains(text(), 'See more')]",
    ]

    clicks = 0
    for xpath in xpaths:
        if clicks >= max_clicks:
            break
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements[:max_clicks]:
                if clicks >= max_clicks:
                    break
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    time.sleep(0.5)
                    el.click()
                    time.sleep(0.8)
                    clicks += 1
                except Exception:
                    continue
        except Exception:
            continue


def extract_visible_post_text(driver: webdriver.Chrome) -> str:
    expand_visible_comments(driver, max_clicks=4)

    chunks = []
    articles = driver.find_elements(By.XPATH, "//div[@role='article']")
    for el in articles[:5]:
        text = (el.text or "").strip()
        if text and len(text) > 30:
            chunks.append(text[:2500])

    if not chunks:
        body_text = (driver.find_element(By.TAG_NAME, "body").text or "").strip()
        chunks.append(body_text[:5000])

    return "\n\n---\n\n".join(chunks)[:6000]


def generate_template_comment(context: str, visible_text: str) -> str:
    """
    Free no-API fallback.
    Uses visible post context to write a more specific Geodo-style comment.
    It returns SKIP when the post does not look relevant.
    """
    import random

    combined = f"{context}\n{visible_text}".lower()

    def has(*terms):
        return any(t in combined for t in terms)

    # Skip obviously unrelated posts.
    relevant_terms = [
        "lead", "leads", "sales", "pipeline", "crm", "saas", "b2b", "gtm",
        "go to market", "outbound", "cold email", "email outreach", "sdr",
        "pricing", "landing page", "customer acquisition", "growth",
        "follow up", "follow-up", "automation", "deal"
    ]

    if not any(term in combined for term in relevant_terms):
        return "SKIP"

    landing_pricing = [
        "The landing page/pricing idea makes sense, but I’d make the post-lead workflow just as clear — how the lead gets qualified, followed up with, and moved through pipeline. That’s usually where B2B teams lose momentum.",
        "One thing I’d think about is what happens after someone becomes a lead. The offer can get attention, but the bigger GTM question is how follow-up, qualification, and pipeline tracking stay consistent.",
        "This feels less like just a lead-gen problem and more like a GTM workflow problem. I’d make sure the landing page clearly connects the offer to what happens next: qualification, outreach, and follow-up."
    ]

    cold_email = [
        "Cold email can work, but the follow-up system matters a lot more than just the first message. The teams that win usually have a clean process for tracking replies, next steps, and pipeline movement.",
        "The tricky part with outbound is keeping it personalized while still making it repeatable. I’d focus on the workflow around the email too — targeting, follow-up, and knowing when a lead is actually ready to move forward.",
        "The prompt/message is only one part of outbound. The bigger unlock is keeping the lead context, follow-up, and pipeline steps connected so good replies don’t get lost."
    ]

    lead_gen = [
        "Getting more leads is helpful, but lead quality and follow-up usually matter more. A cleaner GTM workflow around qualification and pipeline movement can make the same lead volume perform way better.",
        "A lot of teams focus on volume first, but the real bottleneck is usually what happens after the lead comes in. Qualification, outreach, and follow-up need to be tight or the pipeline gets messy fast.",
        "This is where AI can actually be useful if it’s tied to the GTM workflow instead of just generating lists. The goal should be better context, cleaner follow-up, and less manual pipeline work."
    ]

    pipeline_crm = [
        "Pipeline visibility gets messy fast when outreach, CRM notes, and follow-ups live in different places. I’d look at the workflow end-to-end instead of just trying to add another tool on top.",
        "The real issue is usually consistency: who followed up, what was said, and what the next step is. If that context is not centralized, pipeline management becomes way harder than it needs to be.",
        "This is a classic sales ops problem. The best fix is usually not just more data, but a cleaner system for keeping outreach, deal context, and next actions connected."
    ]

    saas_growth = [
        "For SaaS, I’d think about this through the full GTM loop: who the ICP is, what trigger makes them care now, how they’re reached, and how the follow-up gets handled.",
        "The growth angle here depends a lot on whether the team has a repeatable motion after the first touch. Lead gen is good, but the follow-up and deal process are where a lot of SaaS teams leak pipeline.",
        "This seems like the kind of thing where the process matters as much as the tool. Clear ICP, targeted outreach, and consistent follow-up usually beat just adding more volume."
    ]

    geodo_soft = [
        "This is actually close to what Geodo is focused on — making the GTM workflow less manual across lead gen, outreach, pipeline follow-up, and deal coaching. The main value is keeping the process connected instead of scattered.",
        "Geodo is working on this kind of GTM workflow problem: lead gen is only one piece, but the bigger part is keeping outreach, pipeline management, and follow-up consistent for B2B teams.",
        "This is the kind of sales workflow Geodo is trying to help with — not just finding leads, but making the follow-up and pipeline process easier to manage."
    ]

    if has("landing page", "pricing", "tier", "free tier", "paid tier"):
        return random.choice(landing_pricing)

    if has("cold email", "email outreach", "outbound email", "email prompt", "inbox"):
        return random.choice(cold_email)

    if has("crm", "pipeline", "deal", "follow up", "follow-up", "sales ops"):
        return random.choice(pipeline_crm)

    if has("lead", "leads", "lead generation", "qualified leads", "customer acquisition"):
        return random.choice(lead_gen + geodo_soft)

    if has("saas", "startup", "founder", "growth", "gtm", "go to market", "b2b"):
        return random.choice(saas_growth + geodo_soft)

    return random.choice(geodo_soft)


def generate_openai_comment(context: str, visible_text: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        return generate_template_comment(context, visible_text)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        prompt = f"""
You are writing a short, helpful Facebook comment as someone affiliated with Geodo.

{GEODO_CONTEXT}

Rules:
- Respond to the actual post/context first.
- If existing comments are visible, avoid repeating the same thing and add a useful angle.
- Keep it 1–3 sentences.
- Do not sound like an ad.
- Avoid generic praise.
- If the post is unrelated to B2B sales, GTM, lead generation, outbound, SDRs, CRM, sales automation, chatbots, or deal coaching, return exactly SKIP.
- Do not claim personal usage unless the context clearly says that.

CSV context:
{context}

Visible Facebook post and existing comment context:
{visible_text[:3000]}
"""

        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.85,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        print(f"[warn] OpenAI failed, using fallback template: {exc}")
        return generate_template_comment(context, visible_text)


def find_comment_box(driver: webdriver.Chrome, wait_seconds: int = 20):
    wait = WebDriverWait(driver, wait_seconds)

    css_selectors = [
        'div[contenteditable="true"][aria-label*="Write a comment"]',
        'div[contenteditable="true"][aria-label*="Comment as"]',
        'div[contenteditable="true"][aria-label*="Answer as"]',
        'div[role="textbox"][contenteditable="true"]',
    ]

    last_error = None

    for css in css_selectors:
        try:
            box = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, css)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
            time.sleep(0.8)
            if box.is_displayed():
                return box
        except Exception as exc:
            last_error = exc

    text_xpaths = [
        "//*[contains(text(), 'Answer as')]",
        "//*[contains(text(), 'Write a comment')]",
        "//*[contains(text(), 'Comment as')]",
    ]

    for xpath in text_xpaths:
        try:
            el = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.8)
            el.click()
            time.sleep(0.8)
            return driver.switch_to.active_element
        except Exception as exc:
            last_error = exc

    raise TimeoutException(f"Could not find Facebook comment box. Last error: {last_error}")


def type_comment(driver: webdriver.Chrome, comment: str) -> None:
    box = find_comment_box(driver)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
    time.sleep(1)
    box.click()
    time.sleep(1)

    subprocess.run("pbcopy", input=comment, text=True, check=True)

    ActionChains(driver) \
        .key_down(Keys.COMMAND) \
        .send_keys("v") \
        .key_up(Keys.COMMAND) \
        .perform()

    time.sleep(1.5)


def click_send_button(driver: webdriver.Chrome) -> None:
    wait = WebDriverWait(driver, 10)

    xpaths = [
        "//div[@aria-label='Press Enter to post.']",
        "//div[@role='button' and contains(@aria-label, 'Comment')]",
        "//div[@role='button' and contains(@aria-label, 'Send')]",
    ]

    for xpath in xpaths:
        try:
            btn = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
            btn.click()
            return
        except Exception:
            pass

    box = find_comment_box(driver, wait_seconds=5)
    box.send_keys(Keys.ENTER)


def open_in_new_tab(driver: webdriver.Chrome, url: str) -> None:
    driver.switch_to.new_window("tab")
    driver.get(url)


def run_batch(args) -> None:
    load_dotenv()

    rows = [r for r in read_csv(args.csv) if r.status == "pending"][:args.max_comments]
    if not rows:
        print("No pending rows found.")
        return

    driver = setup_driver()
    kept_tabs = []

    try:
        print("\nChrome is opening. If Facebook asks you to log in, log in manually.\n")

        for idx, row in enumerate(rows, start=1):
            print(f"\n[{idx}/{len(rows)}] Opening in new tab: {row.post_url}")

            open_in_new_tab(driver, row.post_url)
            kept_tabs.append(driver.current_window_handle)

            time.sleep(random.uniform(args.min_delay, args.max_delay))

            visible_text = extract_visible_post_text(driver)
            comment = generate_openai_comment(row.context, visible_text)

            if comment.strip().upper() == "SKIP":
                print("Skipped: not relevant enough.")
                write_log(row, comment, "skipped")
                continue

            print("\nGenerated comment:\n")
            print(comment)
            print("\nAuto-typing comment into Facebook...")

            if args.dry_run:
                print("[dry-run] Not typing into Facebook.")
                write_log(row, comment, "dry_run")
                continue

            type_comment(driver, comment)
            print("Typed into Facebook draft.")

            if args.send_after_confirmation:
                confirm = input("Type SEND to post this comment, or press Enter to leave draft: ").strip()
                if confirm == "SEND":
                    click_send_button(driver)
                    print("Submitted.")
                    write_log(row, comment, "submitted_after_confirmation")
                else:
                    print("Left as draft.")
                    write_log(row, comment, "typed_left_as_draft")
            else:
                print("Left as draft. Tab will stay open.")

            write_log(row, comment, "typed_left_as_draft")

            if idx < len(rows):
                cooldown = random.uniform(args.cooldown_min, args.cooldown_max)
                print(f"Cooldown: waiting {round(cooldown)} seconds before opening next tab...")
                time.sleep(cooldown)

        print("\nBatch finished.")
        print(f"Kept {len(kept_tabs)} tabs open for review.")
        input("Review the drafted comments in Chrome. Press Enter here only when you want to close Chrome...")

    finally:
        if args.close_when_done:
            driver.quit()
        else:
            print("Leaving Chrome open. Close it manually when done.")


def parse_args():
    parser = argparse.ArgumentParser(description="Geodo batch Facebook comment assistant")
    parser.add_argument("--csv", default="post_urls.csv")
    parser.add_argument("--max-comments", type=int, default=3)
    parser.add_argument("--min-delay", type=float, default=5)
    parser.add_argument("--max-delay", type=float, default=10)
    parser.add_argument("--cooldown-min", type=float, default=120)
    parser.add_argument("--cooldown-max", type=float, default=180)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send-after-confirmation", action="store_true")
    parser.add_argument("--close-when-done", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    run_batch(parse_args())
