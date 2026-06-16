from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

SELENIUM_IMPORT_ERROR = None
try:
    from selenium import webdriver
    from selenium.common.exceptions import SessionNotCreatedException
    from selenium.common.exceptions import StaleElementReferenceException
    from selenium.webdriver import ChromeOptions
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.remote.webelement import WebElement
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
except ModuleNotFoundError as exc:
    SELENIUM_IMPORT_ERROR = exc
    webdriver = None
    ChromeOptions = None
    Service = None
    ActionChains = None
    By = None
    Keys = None
    WebElement = object
    EC = None
    WebDriverWait = None
    ChromeDriverManager = None

    class StaleElementReferenceException(Exception):
        pass

    class SessionNotCreatedException(Exception):
        pass


STATE_DIR = Path("state")
SEEN_POSTS_PATH = STATE_DIR / "seen_posts.json"
GROUP_STATUS_PATH = STATE_DIR / "group_status.json"
RUN_LOG_PATH = STATE_DIR / "run_log.csv"
DRAFT_QUEUE_PATH = STATE_DIR / "draft_queue.csv"

GROUP_STATUSES = {
    "ok",
    "private_or_join_required",
    "inactive_no_recent_posts",
    "not_commentable",
    "no_matches",
    "drafted",
    "posted",
    "error",
}

BAD_GROUP_STATUSES = {
    "private_or_join_required",
    "inactive_no_recent_posts",
    "not_commentable",
    "no_matches",
}

HEADER_OR_NON_POST_SIGNALS = [
    "about this group",
    "group rules",
    "recent media",
    "featured",
    "members",
    "people joined",
    "created this group",
    "admin assist",
    "group by",
    "public group",
    "private group",
]

STRONG_SIGNALS = [
    "gtm",
    "go to market",
    "sales",
    "lead",
    "leads",
    "lead generation",
    "outbound",
    "cold email",
    "pipeline",
    "crm",
    "saas",
    "b2b",
    "founder",
    "startup",
    "pricing",
    "landing page",
    "traction",
    "first users",
    "first 100 users",
    "conversion",
    "marketing",
    "growth",
    "customer acquisition",
    "client acquisition",
    "follow-up",
    "follow up",
    "deal",
    "demand gen",
    "revenue",
    "revenue growth",
    "business systems",
    "sales systems",
    "systems and processes",
    "business process",
    "business processes",
    "operations",
    "workflow",
    "workflows",
    "automation",
    "business owners",
    "tech services",
    "web apps",
    "ai solutions",
    "potential clients",
    "getting clients",
    "find clients",
    "more clients",
    "what services",
    "services does your business",
    "business provide",
    "business name",
    "agency",
    "recommend tools",
    "what tools",
    "struggling with",
    "stopping your business",
]

RELEVANCE_THRESHOLD = 2


@dataclass
class CandidatePost:
    article: WebElement
    text: str
    post_url: str
    fingerprint: str
    score: int
    matches: List[str]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def normalize_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def post_fingerprint(text: str, post_url: str = "") -> str:
    stable = f"{normalize_url(post_url)}|{normalize_text(text)[:1800]}"
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def setup_driver() -> webdriver.Chrome:
    if SELENIUM_IMPORT_ERROR:
        raise SystemExit(
            "Missing Selenium dependencies. Run `pip install -r requirements.txt` before starting the runner."
        ) from SELENIUM_IMPORT_ERROR

    options = ChromeOptions()
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--start-maximized")
    options.add_experimental_option("detach", True)
    user_data_dir = Path.cwd() / "chrome_data"
    options.add_argument(f"--user-data-dir={user_data_dir}")
    service = Service(ChromeDriverManager().install())
    try:
        return webdriver.Chrome(service=service, options=options)
    except SessionNotCreatedException as exc:
        raise SystemExit(
            "Chrome could not start, usually because an old chromedriver/Chrome profile is still open.\n"
            "Run:\n"
            "  pkill -f chromedriver\n"
            "  pkill -f \"chrome_data\"\n"
            "Then start the runner again."
        ) from exc


def load_keywords(path: str) -> List[str]:
    raw = Path(path).read_text(encoding="utf-8").splitlines()
    return [
        line.strip().lower()
        for line in raw
        if line.strip() and not line.strip().startswith("#")
    ]


def load_group_urls(path: str) -> List[str]:
    urls: List[str] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        has_header = "group_url" in sample.splitlines()[0].lower() if sample else False
        if has_header:
            reader = csv.DictReader(f)
            for row in reader:
                url = (row.get("group_url") or "").strip()
                if url:
                    urls.append(normalize_url(url))
        else:
            for line in f:
                url = line.strip()
                if url and not url.startswith("#"):
                    urls.append(normalize_url(url))
    return list(dict.fromkeys(urls))


def ensure_state_files(reset_state: bool = False) -> Tuple[Dict, Dict]:
    STATE_DIR.mkdir(exist_ok=True)
    if reset_state:
        SEEN_POSTS_PATH.write_text("{}", encoding="utf-8")
        GROUP_STATUS_PATH.write_text("{}", encoding="utf-8")

    if not SEEN_POSTS_PATH.exists():
        SEEN_POSTS_PATH.write_text("{}", encoding="utf-8")
    if not GROUP_STATUS_PATH.exists():
        GROUP_STATUS_PATH.write_text("{}", encoding="utf-8")

    ensure_csv(
        RUN_LOG_PATH,
        ["timestamp", "group_url", "post_url", "status", "reason", "score", "matches"],
    )
    ensure_csv(
        DRAFT_QUEUE_PATH,
        [
            "timestamp",
            "group_url",
            "post_url",
            "fingerprint",
            "draft",
            "status",
            "score",
            "matches",
        ],
    )

    return read_json(SEEN_POSTS_PATH), read_json(GROUP_STATUS_PATH)


def read_json(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def write_json(path: Path, data: Dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def ensure_csv(path: Path, fieldnames: Sequence[str]) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=fieldnames).writeheader()


def append_csv(path: Path, fieldnames: Sequence[str], row: Dict) -> None:
    ensure_csv(path, fieldnames)
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writerow({field: row.get(field, "") for field in fieldnames})


def mark_seen(
    seen_posts: Dict,
    fingerprint: str,
    group_url: str,
    post_url: str,
    status: str,
    reason: str,
    score: int = 0,
    matches: Optional[Sequence[str]] = None,
) -> None:
    seen_posts[fingerprint] = {
        "first_seen": seen_posts.get(fingerprint, {}).get("first_seen", now_iso()),
        "last_seen": now_iso(),
        "group_url": group_url,
        "post_url": normalize_url(post_url),
        "status": status,
        "reason": reason,
        "score": score,
        "matches": list(matches or []),
    }
    write_json(SEEN_POSTS_PATH, seen_posts)


def update_group_status(
    group_status: Dict,
    group_url: str,
    status: str,
    reason: str = "",
    drafts_created: int = 0,
) -> None:
    if status not in GROUP_STATUSES:
        status = "error"
    group_status[group_url] = {
        "last_checked": now_iso(),
        "status": status,
        "reason": reason,
        "drafts_created": drafts_created,
    }
    write_json(GROUP_STATUS_PATH, group_status)


def hours_since(iso_value: str) -> Optional[float]:
    if not iso_value:
        return None
    try:
        checked_at = datetime.fromisoformat(iso_value)
    except ValueError:
        return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - checked_at
    return delta.total_seconds() / 3600


def log_run(
    group_url: str,
    post_url: str,
    status: str,
    reason: str = "",
    score: int = 0,
    matches: Optional[Sequence[str]] = None,
) -> None:
    append_csv(
        RUN_LOG_PATH,
        ["timestamp", "group_url", "post_url", "status", "reason", "score", "matches"],
        {
            "timestamp": now_iso(),
            "group_url": group_url,
            "post_url": post_url,
            "status": status,
            "reason": reason,
            "score": score,
            "matches": ", ".join(matches or []),
        },
    )


def log_draft(candidate: CandidatePost, group_url: str, draft: str, status: str) -> None:
    append_csv(
        DRAFT_QUEUE_PATH,
        [
            "timestamp",
            "group_url",
            "post_url",
            "fingerprint",
            "draft",
            "status",
            "score",
            "matches",
        ],
        {
            "timestamp": now_iso(),
            "group_url": group_url,
            "post_url": candidate.post_url,
            "fingerprint": candidate.fingerprint,
            "draft": draft,
            "status": status,
            "score": candidate.score,
            "matches": ", ".join(candidate.matches),
        },
    )


def debug_print(args, message: str) -> None:
    if args.debug:
        print(f"[debug] {message}")


def visible_body_text(driver: webdriver.Chrome) -> str:
    try:
        return (driver.find_element(By.TAG_NAME, "body").text or "").strip()
    except Exception:
        return ""


def find_articles(driver: webdriver.Chrome) -> List[WebElement]:
    try:
        return [el for el in driver.find_elements(By.CSS_SELECTOR, "div[role='article']") if el.is_displayed()]
    except Exception:
        return []


def scroll_page(driver: webdriver.Chrome, pages: float = 0.9) -> None:
    driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * arguments[0]));", pages)


def click_discussion_if_available(driver: webdriver.Chrome) -> bool:
    xpaths = [
        "//a[contains(@href, '/discussion') and (contains(., 'Discussion') or contains(@aria-label, 'Discussion'))]",
        "//*[self::span or self::div or self::a][normalize-space()='Discussion']",
    ]
    for xpath in xpaths:
        for element in driver.find_elements(By.XPATH, xpath)[:4]:
            try:
                if element.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                    time.sleep(0.4)
                    element.click()
                    time.sleep(2)
                    return True
            except Exception:
                continue
    return False


def page_has_comment_affordance(driver: webdriver.Chrome) -> bool:
    selectors = [
        'div[contenteditable="true"][aria-label*="comment" i]',
        'div[role="textbox"][contenteditable="true"]',
        '[aria-label*="Comment" i]',
    ]
    for selector in selectors:
        try:
            if any(el.is_displayed() for el in driver.find_elements(By.CSS_SELECTOR, selector)[:10]):
                return True
        except Exception:
            continue
    return False


def initial_group_status(driver: webdriver.Chrome, args) -> Tuple[str, str]:
    body = visible_body_text(driver)
    lower = body.lower()
    articles = find_articles(driver)
    debug_print(args, f"initial visible article count: {len(articles)}")

    if "no posts today" in lower and "no posts in the last month" in lower:
        return "inactive_no_recent_posts", "Facebook reports no recent posts."

    if "join group" in lower and not articles and not page_has_comment_affordance(driver):
        return "private_or_join_required", "Join prompt visible and no discussion posts are available."

    if any(signal in lower for signal in ("about this group", "group rules")) and not articles:
        clicked = click_discussion_if_available(driver)
        if clicked:
            articles = find_articles(driver)
        if not articles:
            scroll_page(driver, -0.8)
            time.sleep(1)
            articles = find_articles(driver)
        if not articles:
            return "no_matches", "About/rules content is visible but discussion posts are not."

    return "ok", ""


def extract_post_url(article: WebElement) -> str:
    try:
        links = article.find_elements(By.XPATH, ".//a[@href]")
    except StaleElementReferenceException:
        return ""

    for link in links:
        href = normalize_url(link.get_attribute("href") or "")
        if not href:
            continue
        if "facebook.com/groups/" in href and "/posts/" in href:
            return href
        if "facebook.com/permalink.php" in href:
            return href
    return ""


def is_probably_post_text(text: str) -> bool:
    compact = normalize_text(text)
    if len(compact) < 45:
        return False
    if any(signal in compact for signal in HEADER_OR_NON_POST_SIGNALS) and len(compact) < 500:
        return False
    if compact.count("comment") == 0 and compact.count("like") == 0 and len(compact) < 100:
        return False
    return True


def relevance_score(text: str, keywords: Sequence[str]) -> Tuple[int, List[str]]:
    lower = normalize_text(text)
    matches: List[str] = []
    score = 0

    for keyword in keywords:
        if keyword and keyword in lower and keyword not in matches:
            matches.append(keyword)
            score += 2 if " " in keyword else 1

    for signal in STRONG_SIGNALS:
        if signal in lower and signal not in matches:
            matches.append(signal)
            score += 2 if " " in signal else 1

    business_terms = [
        "business",
        "service",
        "services",
        "clients",
        "customers",
        "systems",
        "process",
        "operations",
        "marketing",
        "sales",
        "growth",
        "automation",
        "software",
        "apps",
        "ai",
    ]
    business_hits = [term for term in business_terms if term in lower]
    if len(business_hits) >= 2 and "business context" not in matches:
        matches.append("business context")
        score += 2

    question_terms = ["how do", "what are", "what's", "looking for", "need help", "any advice", "recommend"]
    if any(term in lower for term in question_terms) and business_hits and "business question" not in matches:
        matches.append("business question")
        score += 1

    return score, matches


def article_has_comment_affordance(article: WebElement) -> bool:
    selectors = [
        'div[contenteditable="true"][aria-label*="comment" i]',
        'div[contenteditable="true"][aria-label*="Comment"]',
        'div[contenteditable="true"][aria-label*="Write"]',
        'div[contenteditable="true"][aria-placeholder*="comment" i]',
        'div[contenteditable="true"][data-lexical-editor="true"]',
        'div[role="textbox"][contenteditable="true"]',
        '[aria-label*="Comment" i]',
        '[aria-label*="Leave a comment" i]',
    ]
    for selector in selectors:
        try:
            for element in article.find_elements(By.CSS_SELECTOR, selector)[:5]:
                if element.is_displayed():
                    return True
        except Exception:
            continue

    xpaths = [
        ".//*[normalize-space()='Comment']",
        ".//*[contains(@aria-label, 'Comment')]",
        ".//*[contains(@aria-label, 'Leave a comment')]",
        ".//*[contains(text(), 'Comment')]",
        ".//*[contains(text(), 'Reply')]",
        ".//*[@role='button' and .//*[contains(text(), 'Comment')]]",
    ]
    for xpath in xpaths:
        try:
            for element in article.find_elements(By.XPATH, xpath)[:6]:
                if element.is_displayed():
                    return True
        except Exception:
            continue
    return False


def detect_candidates(
    driver: webdriver.Chrome,
    keywords: Sequence[str],
    seen_posts: Dict,
    group_url: str,
    args,
) -> Tuple[List[CandidatePost], int]:
    candidates: List[CandidatePost] = []
    not_commentable_count = 0
    articles = find_articles(driver)
    print(f"Visible article count: {len(articles)}")

    for article in articles:
        try:
            text = (article.text or "").strip()
        except StaleElementReferenceException:
            continue

        if not is_probably_post_text(text):
            continue

        post_url = extract_post_url(article)
        fingerprint = post_fingerprint(text, post_url)
        if fingerprint in seen_posts:
            debug_print(args, f"already seen: {fingerprint[:10]}")
            continue

        score, matches = relevance_score(text, keywords)
        snippet = text.replace("\n", " ")[:220]
        print(f"Candidate post snippet: {snippet}")
        print(f"Relevance score: {score}; matched keywords: {', '.join(matches[:10]) or 'none'}")

        if score < RELEVANCE_THRESHOLD:
            continue

        commentable = article_has_comment_affordance(article)
        print(f"Comment box/button found: {'yes' if commentable else 'no'}")

        if not commentable:
            not_commentable_count += 1
            mark_seen(
                seen_posts,
                fingerprint,
                group_url,
                post_url,
                "not_commentable",
                "Relevant-looking post had no visible comment composer or comment button.",
                score,
                matches,
            )
            log_run(group_url, post_url, "not_commentable", "No comment affordance.", score, matches)
            continue

        if not post_url:
            not_commentable_count += 1
            mark_seen(
                seen_posts,
                fingerprint,
                group_url,
                post_url,
                "not_commentable",
                "No stable post URL was available for opening a review tab.",
                score,
                matches,
            )
            log_run(group_url, "", "not_commentable", "Missing stable post URL.", score, matches)
            continue

        candidates.append(CandidatePost(article, text, post_url, fingerprint, score, matches))

    return candidates, not_commentable_count


def click_more_context(article: WebElement, max_clicks: int = 3) -> None:
    xpaths = [
        ".//*[contains(text(), 'See more')]",
        ".//*[contains(text(), 'View more comments')]",
        ".//*[contains(text(), 'View') and contains(text(), 'comments')]",
        ".//*[contains(text(), 'View replies')]",
        ".//*[contains(text(), 'View') and contains(text(), 'replies')]",
    ]
    clicks = 0
    for xpath in xpaths:
        if clicks >= max_clicks:
            return
        try:
            elements = article.find_elements(By.XPATH, xpath)
        except StaleElementReferenceException:
            return
        for element in elements[: max_clicks - clicks]:
            try:
                if element.is_displayed():
                    element.click()
                    clicks += 1
                    time.sleep(0.7)
                    if clicks >= max_clicks:
                        return
            except Exception:
                continue


def extract_context_from_article(article: WebElement) -> Tuple[str, List[str]]:
    click_more_context(article, max_clicks=3)
    text = (article.text or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    original_lines: List[str] = []
    comment_lines: List[str] = []
    comment_mode = False
    skip_words = {"like", "comment", "share", "reply", "send", "author"}

    for line in lines:
        lower = line.lower()
        if "view more comments" in lower or "most relevant" in lower:
            comment_mode = True
            continue
        if lower in skip_words or lower.endswith("replies"):
            continue
        if not comment_mode and len(" ".join(original_lines)) < 1600:
            original_lines.append(line)
        elif len(line) > 20 and len(comment_lines) < 5:
            comment_lines.append(line)

    original = " ".join(original_lines)[:2000] or text[:2000]
    return original, comment_lines[:5]


def local_generate_draft(post_text: str, comments: Sequence[str], matches: Sequence[str]) -> str:
    combined = normalize_text(f"{post_text}\n{' '.join(comments)}")
    if not any(signal in combined for signal in STRONG_SIGNALS) and "business context" not in matches:
        return "SKIP"

    def has(*terms: str) -> bool:
        return any(term in combined for term in terms)

    existing_comment_text = normalize_text(" ".join(comments))
    seed = int(hashlib.sha256(combined[:1200].encode("utf-8")).hexdigest()[:8], 16)

    options: List[str]
    angle = "general"
    if has("gohighlevel", "ghl", "snapshot", "appointment", "calendar", "sms campaign"):
        angle = "ghl_agency_ops"
        options = [
            "GHL support is one of those things where the small backend details really decide whether the agency feels organized or chaotic. The CRM, automations, follow-ups, and handoffs all have to line up or leads slip through.",
            "This is a solid backend ops offer. The part I’d care about most is whether the CRM updates, automations, calendar flow, and lead nurturing are all connected cleanly once volume picks up.",
            "The GHL piece is useful because most agencies do not just need setup, they need the follow-up and pipeline motion to stay clean after new leads come in. That connected workflow is where tools like Geodo are focused too.",
            "For agency owners, this kind of backend work can make a real difference. If CRM hygiene, automation setup, and lead follow-up are not tight, even good campaigns start leaking opportunities.",
        ]
    elif has("seo", "organic traffic", "content strategy", "website messaging", "ux", "search", "monthly visitors"):
        angle = "saas_content_growth"
        options = [
            "The organic growth angle is strong, especially if the content is tied to conversion and follow-up instead of just traffic. For SaaS, the real win is turning the right visitors into qualified conversations.",
            "Traffic growth is great, but I like that this connects it back to leads and funnel optimization. That handoff from content interest to actual sales follow-up is where a lot of SaaS teams lose momentum.",
            "This is the kind of SaaS growth work that gets more useful when it plugs into the rest of the GTM motion. Content brings people in, but the follow-up and pipeline process decide how much of it turns into revenue.",
            "For SaaS teams, SEO and content can work really well when the messaging, funnel, and lead follow-up are designed together. Otherwise you can get traffic without enough pipeline movement.",
        ]
    elif has("cold email", "outbound", "email outreach"):
        angle = "outbound"
        options = [
            "Cold email can work, but the follow-up system matters more than the first message. I’d make sure targeting, reply context, and next steps stay connected so good conversations do not get lost.",
            "For outbound, I’d think about the full loop: who you target, what trigger makes the message relevant, and how follow-up gets handled once someone replies. That process usually beats just sending more volume.",
            "The part that usually makes outbound work is not just the opener, it is what happens after the reply. Keeping lead context, follow-up, and pipeline steps connected is the boring part that actually matters.",
            "I’d pay just as much attention to the reply workflow as the campaign itself. Good targeting creates conversations, but the follow-up process is what turns those conversations into pipeline.",
        ]
    elif has("landing page", "pricing", "conversion"):
        angle = "conversion"
        options = [
            "I’d pair the landing page/pricing work with a really clear post-lead workflow. The page can create intent, but qualification and follow-up are usually where B2B teams either create pipeline or lose momentum.",
            "One useful angle is mapping what happens after someone converts: who follows up, what context they get, and how the deal moves forward. That GTM loop matters as much as the page itself.",
            "The landing page can only do part of the job. I’d make sure the form, lead context, follow-up, and pipeline step are all designed together so interest does not just sit there.",
            "Conversion work gets a lot better when it is connected to sales execution. Messaging can create the lead, but the next touch and qualification process decide whether it becomes real pipeline.",
        ]
    elif has("crm", "pipeline", "deal", "follow-up", "follow up"):
        angle = "pipeline_crm"
        options = [
            "The pipeline piece is usually where this gets messy. If outreach, CRM notes, and follow-up live in different places, the team can have demand and still miss the next step.",
            "I’d look at the workflow end to end here. Better leads help, but the bigger win is keeping context, follow-up, and pipeline movement connected once conversations start.",
            "CRM only helps if the next action is obvious. The teams that keep notes, replies, follow-ups, and deal stages connected usually get more out of the same lead flow.",
            "This is where process matters more than another dashboard. If follow-ups and pipeline context are not kept clean, a lot of good conversations quietly go nowhere.",
        ]
    elif has("lead", "leads", "lead generation", "customer acquisition"):
        angle = "lead_generation"
        options = [
            "Getting leads is only part of the problem. I’d focus just as much on lead quality, reply handling, and the follow-up process, because that’s where a lot of early pipeline quietly leaks.",
            "This is where a tighter GTM workflow helps: lead gen, outreach, follow-up, and pipeline tracking all need to stay connected. That’s the kind of problem Geodo is focused on for B2B teams.",
            "I’d think about this as a full lead-to-pipeline problem. More leads help, but the bigger lift usually comes from better qualification, faster follow-up, and not losing context between steps.",
            "Lead generation gets more valuable when the team has a clean way to handle replies and next steps. That is the workflow Geodo is trying to make less scattered for B2B sales teams.",
        ]
    elif has("business systems", "sales systems", "systems and processes", "operations", "workflow", "automation"):
        angle = "systems_workflow"
        options = [
            "The systems/process side matters a lot once a business starts getting more conversations. Geodo is focused on that GTM workflow for B2B teams: lead gen, outreach, follow-up, and pipeline context staying in one place.",
            "I’d look at this less as one isolated tool and more as the full workflow: where leads come from, how follow-up happens, and how the next step gets tracked. That’s the kind of connected process Geodo is built around.",
            "Systems get important fast when the business is growing. It is usually not just automation for its own sake, it is making sure leads, conversations, and next steps do not get scattered.",
            "The process angle is underrated. Once marketing and sales activity picks up, having the lead source, follow-up, and pipeline stage connected can save a lot of messy manual work.",
        ]
    elif has("what services", "business provide", "tech services", "web apps", "ai solutions", "business name", "potential clients"):
        angle = "business_services"
        options = [
            "Geodo is building in the B2B GTM space: lead generation, outreach, follow-up, and pipeline workflows for sales teams. The main idea is helping teams keep the whole process connected once potential clients start responding.",
            "On our side, Geodo is focused on B2B sales workflows: finding leads, managing outreach, keeping follow-up organized, and making pipeline context easier to act on. Still early, but that connected GTM loop is the core.",
            "Geodo is in the B2B sales workflow lane. Think lead generation, outreach, follow-up, and pipeline context staying connected so teams are not jumping between a bunch of disconnected steps.",
            "We are working on Geodo for B2B teams that want the sales motion to feel less scattered: leads, outreach, follow-up, and pipeline visibility in one connected workflow.",
        ]
    else:
        options = [
            "For early traction, I’d think less about just picking one channel and more about keeping the GTM loop tight once people start responding. That’s the kind of workflow Geodo is focused on for B2B teams: lead gen, outreach, follow-up, and pipeline staying connected.",
            "The useful angle here is making the motion repeatable without making it generic. Clear ICP, targeted outreach, and consistent follow-up usually matter more than simply adding more tools or volume.",
            "The thing I’d watch is the handoff between interest and follow-up. A lot of teams can create activity, but the real leverage is keeping the next step clear once someone shows intent.",
            "This feels like a workflow problem as much as a growth problem. If the team can keep lead context, outreach, and follow-up connected, the same effort usually produces cleaner pipeline.",
        ]

    rotated = options[seed % len(options):] + options[:seed % len(options)]
    for draft in rotated:
        if normalize_text(draft)[:90] not in existing_comment_text:
            print(f"[local-generator] angle={angle}")
            return draft
    print(f"[local-generator] angle={angle}")
    return rotated[0]


def openai_generate_draft(post_text: str, comments: Sequence[str], matches: Sequence[str]) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return local_generate_draft(post_text, comments, matches)

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        response = client.chat.completions.create(
            model=model,
            temperature=0.7,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Write one short, natural Facebook comment for a human to review. "
                        "Never include a link by default. Mention Geodo softly only when relevant. "
                        "Avoid hype, spam, generic praise, and repeating existing comments. "
                        "Return exactly SKIP if the post is not clearly about B2B sales, GTM, "
                        "lead generation, outbound, CRM, SaaS, growth, pricing, pipeline, "
                        "business systems, client acquisition, operations, automation, or business services."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Matched keywords/signals: {', '.join(matches)}\n\n"
                        f"Original post:\n{post_text[:2400]}\n\n"
                        f"Visible existing comments/replies, avoid repeating them:\n"
                        f"{chr(10).join('- ' + c for c in comments[:5])}"
                    ),
                },
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        print(f"[warn] OpenAI generation failed, using local generator: {exc}")
        return local_generate_draft(post_text, comments, matches)


def find_target_article(driver: webdriver.Chrome, expected_text: str) -> Optional[WebElement]:
    expected_tokens = set(normalize_text(expected_text).split()[:80])
    best_article = None
    best_overlap = 0
    for article in find_articles(driver):
        try:
            text = article.text or ""
        except StaleElementReferenceException:
            continue
        tokens = set(normalize_text(text).split()[:140])
        overlap = len(expected_tokens & tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best_article = article
    return best_article or (find_articles(driver)[0] if find_articles(driver) else None)


def find_comment_box_in(elements_root, allow_generic_editor: bool = False) -> Optional[WebElement]:
    selectors = [
        'div[contenteditable="true"][aria-label*="comment" i]',
        'div[contenteditable="true"][aria-label*="Comment"]',
        'div[contenteditable="true"][aria-label*="Write"]',
        'div[contenteditable="true"][aria-placeholder*="comment" i]',
        'div[role="textbox"][contenteditable="true"]',
        '[role="textbox"][aria-label*="comment" i]',
        '[role="textbox"][aria-placeholder*="comment" i]',
    ]
    if allow_generic_editor:
        selectors.append('div[contenteditable="true"][data-lexical-editor="true"]')
    for selector in selectors:
        try:
            boxes = elements_root.find_elements(By.CSS_SELECTOR, selector)
        except StaleElementReferenceException:
            return None
        except Exception:
            continue
        for box in boxes:
            try:
                if box.is_displayed():
                    return box
            except Exception:
                continue
    return None


def open_comment_box(driver: webdriver.Chrome, article: WebElement) -> Optional[WebElement]:
    box = find_comment_box_in(article, allow_generic_editor=True) or find_comment_box_in(driver)
    if box:
        return box

    xpaths = [
        ".//*[normalize-space()='Comment']",
        ".//*[contains(@aria-label, 'Comment')]",
        ".//*[contains(@aria-label, 'Leave a comment')]",
        ".//*[contains(text(), 'Comment')]",
        ".//*[contains(text(), 'Reply')]",
        ".//*[@role='button' and .//*[contains(text(), 'Comment')]]",
    ]
    for xpath in xpaths:
        try:
            elements = article.find_elements(By.XPATH, xpath)
        except StaleElementReferenceException:
            return None
        for element in elements[:8]:
            try:
                if element.is_displayed():
                    element.click()
                    time.sleep(1)
                    box = find_comment_box_in(article, allow_generic_editor=True) or find_comment_box_in(driver)
                    if box:
                        return box
            except Exception:
                continue
    return None


def paste_draft(driver: webdriver.Chrome, box: WebElement, draft: str) -> bool:
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
    time.sleep(0.5)
    box.click()
    time.sleep(0.5)
    subprocess.run(["pbcopy"], input=draft, text=True, check=True)
    ActionChains(driver).key_down(Keys.COMMAND).send_keys("v").key_up(Keys.COMMAND).perform()
    time.sleep(1)
    try:
        typed_text = (box.text or box.get_attribute("innerText") or "").strip()
    except Exception:
        typed_text = ""
    if not typed_text:
        try:
            active = driver.switch_to.active_element
            typed_text = (active.text or active.get_attribute("innerText") or "").strip()
        except Exception:
            typed_text = ""
    return normalize_text(draft[:30])[:12] in normalize_text(typed_text)


def comment_box_text(driver: webdriver.Chrome, box: WebElement) -> str:
    try:
        return (box.text or box.get_attribute("innerText") or "").strip()
    except Exception:
        try:
            active = driver.switch_to.active_element
            return (active.text or active.get_attribute("innerText") or "").strip()
        except Exception:
            return ""


def draft_still_present(driver: webdriver.Chrome, box: WebElement, draft: str) -> bool:
    needle = normalize_text(draft[:40])[:16]
    deadline = time.time() + 4
    while time.time() < deadline:
        current = normalize_text(comment_box_text(driver, box))
        if not current or needle not in current:
            return False
        time.sleep(0.5)
    return True


def click_or_js_click(driver: webdriver.Chrome, element: WebElement) -> bool:
    try:
        element.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", element)
            return True
        except Exception:
            return False


def submit_comment(driver: webdriver.Chrome, box: WebElement, draft: str) -> bool:
    """Click Facebook's nearby comment submit control, falling back to Enter."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
    time.sleep(0.4)
    try:
        box.click()
    except Exception:
        pass

    try:
        container = driver.execute_script(
            "return arguments[0].closest('[role=\"dialog\"], [role=\"article\"], form') || document.body;",
            box,
        )
    except Exception:
        container = driver

    scopes = [container, driver] if container else [driver]
    buttons: List[WebElement] = []
    for scope in scopes:
        try:
            buttons.extend(scope.find_elements(By.XPATH, ".//*[@role='button']"))
        except Exception:
            continue

    try:
        box_rect = driver.execute_script(
            "const r = arguments[0].getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height};",
            box,
        )
    except Exception:
        box_rect = {"x": 0, "y": 0, "w": 0, "h": 0}
    box_center_x = box_rect["x"] + box_rect["w"] / 2
    box_center_y = box_rect["y"] + box_rect["h"] / 2

    scored = []
    seen_ids = set()
    for button in buttons:
        try:
            internal_id = button.id
            if internal_id in seen_ids:
                continue
            seen_ids.add(internal_id)
            if not button.is_displayed() or not button.is_enabled():
                continue
            label = normalize_text(
                " ".join(
                    [
                        button.get_attribute("aria-label") or "",
                        button.get_attribute("title") or "",
                        button.text or "",
                    ]
                )
            )
            excluded = ["emoji", "gif", "sticker", "photo", "camera", "avatar", "insert", "attach"]
            if any(term in label for term in excluded):
                continue
            aria_disabled = (button.get_attribute("aria-disabled") or "").lower()
            if aria_disabled == "true" or button.get_attribute("disabled"):
                continue

            rect = driver.execute_script(
                "const r = arguments[0].getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height};",
                button,
            )
            center_x = rect["x"] + rect["w"] / 2
            center_y = rect["y"] + rect["h"] / 2
            near_composer = (
                box_rect["y"] - 60 <= center_y <= box_rect["y"] + box_rect["h"] + 120
                and center_x >= box_rect["x"] + box_rect["w"] * 0.45
            )
            label_says_submit = any(term in label for term in ["send", "post", "comment", "reply"])
            if not near_composer and not label_says_submit:
                continue

            distance = abs(center_x - box_center_x) + abs(center_y - box_center_y)
            label_bonus = -1000 if label_says_submit else 0
            scored.append((label_bonus + distance, button, label or "near composer button"))
        except Exception:
            continue

    scored.sort(key=lambda item: item[0])
    for _, button, label in scored[:6]:
        print(f"Trying auto-submit control: {label}")
        if click_or_js_click(driver, button):
            if not draft_still_present(driver, box, draft):
                return True
            print("Submit control did not clear the draft; trying another candidate.")

    try:
        box.click()
        ActionChains(driver).send_keys(Keys.ENTER).perform()
        return not draft_still_present(driver, box, draft)
    except Exception:
        return False


def type_draft_in_review_tab(
    driver: webdriver.Chrome,
    candidate: CandidatePost,
    group_url: str,
    seen_posts: Dict,
    args,
) -> bool:
    print(f"Opening review tab for post: {candidate.post_url}")
    driver.switch_to.new_window("tab")
    review_handle = driver.current_window_handle
    try:
        driver.get(candidate.post_url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(random.uniform(args.post_load_min, args.post_load_max))

        article = find_target_article(driver, candidate.text)
        if not article:
            mark_seen(
                seen_posts,
                candidate.fingerprint,
                group_url,
                candidate.post_url,
                "not_commentable",
                "Could not locate the target post in review tab.",
                candidate.score,
                candidate.matches,
            )
            return False

        original_post, comments = extract_context_from_article(article)
        draft = openai_generate_draft(original_post, comments, candidate.matches)
        print(f"Generated draft: {draft}")

        if draft.strip().upper() == "SKIP":
            mark_seen(
                seen_posts,
                candidate.fingerprint,
                group_url,
                candidate.post_url,
                "skipped",
                "Generator returned SKIP.",
                candidate.score,
                candidate.matches,
            )
            log_draft(candidate, group_url, draft, "skipped")
            return False

        box = open_comment_box(driver, article)
        print(f"Comment box found in review tab: {'yes' if box else 'no'}")
        if not box:
            mark_seen(
                seen_posts,
                candidate.fingerprint,
                group_url,
                candidate.post_url,
                "not_commentable",
                "No comment box opened in the review tab.",
                candidate.score,
                candidate.matches,
            )
            return False

        if not paste_draft(driver, box, draft):
            mark_seen(
                seen_posts,
                candidate.fingerprint,
                group_url,
                candidate.post_url,
                "error",
                "Clipboard paste did not appear to type a draft.",
                candidate.score,
                candidate.matches,
            )
            return False

        if args.auto_submit:
            print("Draft typed. Auto-submit enabled, submitting comment now.")
            if submit_comment(driver, box, draft):
                args.last_action_was_sent = True
                mark_seen(
                    seen_posts,
                    candidate.fingerprint,
                    group_url,
                    candidate.post_url,
                    "posted",
                    "Comment auto-posted after draft generation.",
                    candidate.score,
                    candidate.matches,
                )
                log_draft(candidate, group_url, draft, "auto_posted")
                log_run(
                    group_url,
                    candidate.post_url,
                    "posted",
                    "Auto-posted successfully.",
                    candidate.score,
                    candidate.matches,
                )
                print("Comment posted successfully. Continuing after cooldown.")
                return True
            print("Auto-submit failed. Leaving the typed draft open for manual review.")

        mark_seen(
            seen_posts,
            candidate.fingerprint,
            group_url,
            candidate.post_url,
            "drafted",
            "Draft typed and left for human review.",
            candidate.score,
            candidate.matches,
        )
        log_draft(candidate, group_url, draft, "typed_left_for_review")
        log_run(group_url, candidate.post_url, "drafted", "Draft typed successfully.", candidate.score, candidate.matches)

        print("Draft typed successfully. Leaving this tab open for review and continuing.")
        return True
    except Exception as exc:
        print(f"Skipped reason: review tab failed: {exc}")
        mark_seen(
            seen_posts,
            candidate.fingerprint,
            group_url,
            candidate.post_url,
            "error",
            f"Review tab failed: {exc}",
            candidate.score,
            candidate.matches,
        )
        log_run(group_url, candidate.post_url, "error", str(exc), candidate.score, candidate.matches)
        return False
    finally:
        if not driver.current_window_handle == review_handle:
            try:
                driver.switch_to.window(review_handle)
            except Exception:
                pass


def close_current_tab_and_return(driver: webdriver.Chrome, scanner_tab: str) -> None:
    try:
        if driver.current_window_handle != scanner_tab:
            driver.close()
    except Exception:
        pass
    driver.switch_to.window(scanner_tab)


def scan_group(
    driver: webdriver.Chrome,
    scanner_tab: str,
    group_url: str,
    group_index: int,
    group_count: int,
    keywords: Sequence[str],
    seen_posts: Dict,
    group_status: Dict,
    open_draft_tabs: List[str],
    args,
) -> int:
    print(f"\nOpening group {group_index}/{group_count}: {group_url}")
    driver.switch_to.window(scanner_tab)
    driver.get(group_url)
    time.sleep(random.uniform(args.group_load_min, args.group_load_max))

    status, reason = initial_group_status(driver, args)
    print(f"Group status: {status}{f' - {reason}' if reason else ''}")
    if status != "ok":
        update_group_status(group_status, group_url, status, reason)
        log_run(group_url, "", status, reason)
        print(f"Moving to next group. Skipped reason: {reason or status}")
        return 0
    update_group_status(group_status, group_url, "ok", "Scanning started.")

    empty_scrolls = 0
    drafted = 0
    not_commentable_seen = 0
    min_scrolls = min(args.min_scrolls_per_group, args.max_scrolls_per_group)

    for scroll_num in range(1, args.max_scrolls_per_group + 1):
        print(f"Scroll {scroll_num}/{args.max_scrolls_per_group}")
        candidates, not_commentable_count = detect_candidates(driver, keywords, seen_posts, group_url, args)
        not_commentable_seen += not_commentable_count

        if candidates:
            empty_scrolls = 0
        else:
            empty_scrolls += 1

        for candidate in candidates:
            if len(open_draft_tabs) >= args.max_open_draft_tabs:
                print(f"Reached --max-open-draft-tabs ({args.max_open_draft_tabs}).")
                args.stop_requested = True
                return drafted

            success = type_draft_in_review_tab(driver, candidate, group_url, seen_posts, args)
            if success:
                if args.last_action_was_sent:
                    close_current_tab_and_return(driver, scanner_tab)
                else:
                    open_draft_tabs.append(driver.current_window_handle)
                drafted += 1
                status = "posted" if args.last_action_was_sent else "drafted"
                reason = "One comment posted." if args.last_action_was_sent else "One draft created."
                update_group_status(group_status, group_url, status, reason, drafted)
                print(f"Moving to next group after successful {status}.")
                driver.switch_to.window(scanner_tab)
                return drafted

            if args.close_skipped_tabs:
                close_current_tab_and_return(driver, scanner_tab)
            else:
                driver.switch_to.window(scanner_tab)

        if scroll_num >= min_scrolls and empty_scrolls >= args.empty_scroll_limit:
            articles = find_articles(driver)
            status = "not_commentable" if not_commentable_seen and articles else "no_matches"
            reason = (
                "Relevant posts were found but none had a usable comment box or stable review URL."
                if status == "not_commentable"
                else f"No usable matches after {empty_scrolls} empty scrolls and {scroll_num} total scrolls."
            )
            update_group_status(group_status, group_url, status, reason)
            log_run(group_url, "", status, reason)
            print(f"Moving to next group. Skipped reason: {reason}")
            return drafted

        driver.switch_to.window(scanner_tab)
        scroll_page(driver, 0.95)
        time.sleep(random.uniform(args.scroll_delay_min, args.scroll_delay_max))

    reason = f"Reached max scrolls ({args.max_scrolls_per_group}) without a usable draft."
    final_status = "not_commentable" if not_commentable_seen else "no_matches"
    if final_status == "not_commentable":
        reason = "Relevant posts were found but none had a usable comment box or stable review URL."
    update_group_status(group_status, group_url, final_status, reason)
    log_run(group_url, "", final_status, reason)
    print(f"Moving to next group. Skipped reason: {reason}")
    return drafted


def should_skip_group(group_status: Dict, group_url: str, args) -> Tuple[bool, str]:
    if args.repeat:
        return False, ""
    record = group_status.get(group_url) or {}
    status = record.get("status", "")
    checked_hours_ago = hours_since(record.get("last_checked", ""))
    if checked_hours_ago is not None and checked_hours_ago < args.group_revisit_hours:
        return True, (
            f"checked {checked_hours_ago:.1f}h ago with status {status or 'unknown'} "
            f"(cooldown {args.group_revisit_hours:g}h)"
        )
    if status in BAD_GROUP_STATUSES:
        return True, f"previous status is {status}"
    return False, ""


def apply_fast_test(args) -> None:
    if not args.fast_test:
        return
    args.cooldown_min = 5
    args.cooldown_max = 8
    args.group_load_min = 2
    args.group_load_max = 3
    args.post_load_min = 2
    args.post_load_max = 3
    args.scroll_delay_min = 0.8
    args.scroll_delay_max = 1.4
    args.min_scrolls_per_group = max(args.min_scrolls_per_group, 15)
    args.max_scrolls_per_group = max(args.max_scrolls_per_group, args.min_scrolls_per_group)
    args.empty_scroll_limit = max(args.empty_scroll_limit, 15)


def run(args) -> None:
    load_dotenv()
    apply_fast_test(args)

    if args.reset_state:
        ensure_state_files(reset_state=True)
        print("Reset state/seen_posts.json and state/group_status.json.")
        return

    keywords = load_keywords(args.keywords)
    group_urls = load_group_urls(args.groups)
    seen_posts, group_status = ensure_state_files(reset_state=False)
    if args.shuffle_groups:
        random.shuffle(group_urls)
    if args.max_groups_per_run > 0:
        group_urls = group_urls[: args.max_groups_per_run]

    if not group_urls:
        raise SystemExit(f"No group URLs found in {args.groups}")
    if not keywords:
        raise SystemExit(f"No keywords found in {args.keywords}")

    driver = setup_driver()
    open_draft_tabs: List[str] = []
    total_drafts = 0
    args.stop_requested = False
    args.last_action_was_sent = False

    print("\nReliable Geodo Facebook draft assistant starting.")
    if args.auto_submit:
        print("AUTO-SUBMIT mode is on. It will click the Facebook comment send/post control after drafting.")
    else:
        print("Draft-only mode is on. It will not press Enter or click the Facebook send/post button.")
    if args.approve_before_send:
        print("--approve-before-send is deprecated and ignored. Use --auto-submit for explicit auto-post mode.")
    print("Make sure Chrome is logged into Facebook before scanning begins.\n")

    try:
        scanner_tab = driver.current_window_handle
        visited_this_run = set()

        for index, group_url in enumerate(group_urls, start=1):
            if total_drafts >= args.max_drafts:
                if args.auto_submit:
                    print(f"Reached --max-drafts ({args.max_drafts}).")
                else:
                    print(f"Reached --max-drafts ({args.max_drafts}). Leaving draft tabs open for review.")
                return

            if group_url in visited_this_run and not args.repeat:
                print(f"Skipping duplicate group in this run: {group_url}")
                continue
            visited_this_run.add(group_url)

            skip, skip_reason = should_skip_group(group_status, group_url, args)
            if skip:
                print(f"\nOpening group {index}/{len(group_urls)}: {group_url}")
                print(f"Group status: skipped - {skip_reason}")
                log_run(group_url, "", "skipped", skip_reason)
                print("Moving to next group.")
                continue

            before = total_drafts
            args.last_action_was_sent = False
            created = scan_group(
                driver,
                scanner_tab,
                group_url,
                index,
                len(group_urls),
                keywords,
                seen_posts,
                group_status,
                open_draft_tabs,
                args,
            )
            total_drafts += created

            if args.stop_requested:
                print("Stopping because the maximum number of open draft tabs is already in use.")
                return

            if created and total_drafts < args.max_drafts:
                cooldown = random.uniform(args.cooldown_min, args.cooldown_max)
                print(f"Cooldown: waiting {round(cooldown)} seconds before the next group.")
                time.sleep(cooldown)

            if total_drafts == before:
                status_record = group_status.get(group_url, {})
                if status_record.get("status") in {"", "ok"}:
                    update_group_status(group_status, group_url, "no_matches", "No draft created.")

        label = "posts/comments completed" if args.auto_submit else "drafts created"
        print(f"\nFinished one pass through group list. {label.title()}: {total_drafts}.")
        if not args.auto_submit:
            print("Review any open draft tabs manually.")
    except KeyboardInterrupt:
        print("\nStopped by Control+C.")
        if not args.auto_submit:
            print("Review any open draft tabs manually.")
    finally:
        if args.close_when_done:
            driver.quit()
        else:
            print("Chrome remains open for review.")


def parse_args():
    parser = argparse.ArgumentParser(description="Reliable Geodo Facebook group draft and optional auto-submit assistant")
    parser.add_argument("--groups", default="group_urls.csv")
    parser.add_argument("--keywords", default="keywords.txt")
    parser.add_argument("--max-drafts", type=int, default=5)
    parser.add_argument("--max-scrolls-per-group", type=int, default=60)
    parser.add_argument("--min-scrolls-per-group", type=int, default=15)
    parser.add_argument("--empty-scroll-limit", type=int, default=15)
    parser.add_argument("--cooldown-min", type=float, default=120)
    parser.add_argument("--cooldown-max", type=float, default=180)
    parser.add_argument("--fast-test", action="store_true")
    parser.add_argument("--repeat", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--group-revisit-hours", type=float, default=72)
    parser.add_argument("--max-groups-per-run", type=int, default=0)
    parser.add_argument("--shuffle-groups", dest="shuffle_groups", action="store_true", default=True)
    parser.add_argument("--no-shuffle-groups", dest="shuffle_groups", action="store_false")
    parser.add_argument("--approve-before-send", action="store_true")
    parser.add_argument("--auto-submit", action="store_true")
    parser.add_argument("--max-open-draft-tabs", type=int, default=5)
    parser.add_argument("--close-skipped-tabs", dest="close_skipped_tabs", action="store_true", default=True)
    parser.add_argument("--no-close-skipped-tabs", dest="close_skipped_tabs", action="store_false")

    parser.add_argument("--group-load-min", type=float, default=4)
    parser.add_argument("--group-load-max", type=float, default=7)
    parser.add_argument("--post-load-min", type=float, default=4)
    parser.add_argument("--post-load-max", type=float, default=7)
    parser.add_argument("--scroll-delay-min", type=float, default=2)
    parser.add_argument("--scroll-delay-max", type=float, default=4)
    parser.add_argument("--close-when-done", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
