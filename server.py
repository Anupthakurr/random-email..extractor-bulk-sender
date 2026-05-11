import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from fastapi import FastAPI, BackgroundTasks, HTTPException, Form, File, UploadFile
import json
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import re
import asyncio
import os
import uvicorn
from typing import List, Set, Dict, TypedDict
from urllib.parse import urlparse, urljoin, quote
from bs4 import BeautifulSoup, Tag
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import random
import base64
import sendgrid
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

app = FastAPI(title="Student Email Extractor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')

BLOCKED_DOMAINS = [
    "example.com", "test.com", "dummy.com", "placeholder.com",
    "sentry.io", "github.com", "githubusercontent.com", "noreply",
    "wixpress.com", "jquery.com", "npmjs.com", "webpack.js",
    "babel.io", "eslint.org", "schema.org", "w3.org", "mozilla.org",
    "apache.org", "google.com", "microsoft.com", "amazon.com",
    "cloudflare.com", "fastly.net", "jsdelivr.net", "unpkg.com",
    "cdnjs.com", "bootstrapcdn.com", "fontawesome.com",
]

class SearchState(TypedDict):
    running: bool
    emails: Set[str]
    email_details: Dict[str, dict]
    progress: int
    log: List[str]
    stats: Dict[str, int]
    search_count: int  # increments each run to offset pagination

sessions_state: Dict[str, SearchState] = {}

def get_state(session_id: str) -> SearchState:
    if session_id not in sessions_state:
        sessions_state[session_id] = {
            "running": False,
            "emails": set(),
            "email_details": {},
            "progress": 0,
            "log": [],
            "stats": {"github": 0, "google": 0, "bing": 0, "devfolio": 0, "gitlab": 0, "npm": 0, "pypi": 0, "devto": 0},
            "search_count": 0
        }
    return sessions_state[session_id]

class CampaignState(TypedDict):
    running: bool
    sent: int
    failed: int
    total: int
    progress: int
    log: List[str]

campaigns_state: Dict[str, CampaignState] = {}

def get_campaign_state(session_id: str) -> CampaignState:
    if session_id not in campaigns_state:
        campaigns_state[session_id] = {
            "running": False,
            "sent": 0,
            "failed": 0,
            "total": 0,
            "progress": 0,
            "log": []
        }
    return campaigns_state[session_id]

def log_campaign(session_id: str, msg: str):
    state = get_campaign_state(session_id)
    state["log"].append(msg)
    if len(state["log"]) > 200:
        state["log"] = state["log"][-200:]
    print(f"[Campaign {session_id}] {msg}")

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class SearchRequest(BaseModel):
    session_id: str
    keyword: str
    sources: List[str]
    max_results: int = 500

class SMTPSender(BaseModel):
    email: str
    password: str

class CampaignRequest(BaseModel):
    session_id: str
    send_method: str = "smtp" # "smtp" or "sendgrid"
    sg_api_key: str = ""
    sg_sender: str = ""
    smtp_server: str = ""
    smtp_port: int = 587
    senders: List[SMTPSender] = []
    subject: str
    body: str
    min_delay: int
    max_delay: int

def is_valid_email(email: str) -> bool:
    email = email.lower()
    if any(b in email for b in BLOCKED_DOMAINS):
        return False
    if email.endswith((".png", ".jpg", ".svg", ".gif", ".css", ".js")):
        return False
    if any(x in email for x in ["your-email", "example", "user@", "admin@host", "test@"]):
        return False
    parts = email.split("@")
    if len(parts) != 2 or len(parts[0]) < 2 or len(parts[1]) < 4:
        return False
    if "." not in parts[1]:
        return False

    local = parts[0]

    # Reject excessively long local parts (real emails are rarely > 30 chars)
    if len(local) > 30:
        return False

    # Reject random-looking tokens: too many digits relative to length
    digit_ratio = sum(c.isdigit() for c in local) / max(len(local), 1)
    if digit_ratio > 0.4:  # more than 40% digits → likely a token/hash
        return False

    # Reject strings with very low vowel ratio (hash/base64 strings have almost no vowels)
    vowels = set("aeiou")
    alpha_chars = [c for c in local if c.isalpha()]
    if len(alpha_chars) >= 8:
        vowel_ratio = sum(c in vowels for c in alpha_chars) / len(alpha_chars)
        if vowel_ratio < 0.1:  # less than 10% vowels → looks like a hash
            return False

    return True


def add_email(session_id: str, email: str, source: str, name: str = "", platform: str = "") -> bool:
    state = get_state(session_id)
    email = email.lower().strip()
    if is_valid_email(email) and email not in state["emails"]:
        state["emails"].add(email)
        state["email_details"][email] = {
            "email": email,
            "source": source,
            "name": name,
            "platform": platform
        }
        state["stats"][source] = state["stats"].get(source, 0) + 1
        return True
    return False


def log(session_id: str, msg: str):
    state = get_state(session_id)
    state["log"].append(msg)
    if len(state["log"]) > 200:
        state["log"] = state["log"][-200:]
    print(f"[{session_id}] {msg}")


def extract_emails_from_text(text: str) -> List[str]:
    """Extract emails, also handle obfuscated ones like name [at] domain [dot] com"""
    # Standard emails
    found = set(EMAIL_REGEX.findall(text))
    # Obfuscated: name [at] domain [dot] com
    obfuscated = re.findall(
        r'([A-Za-z0-9._%+\-]+)\s*[\[\(]?\s*at\s*[\]\)]?\s*([A-Za-z0-9.\-]+)\s*[\[\(]?\s*dot\s*[\]\)]?\s*([A-Za-z]{2,})',
        text, re.IGNORECASE
    )
    for parts in obfuscated:
        email = f"{parts[0]}@{parts[1]}.{parts[2]}"
        found.add(email)
    return list(found)


def scrape_page_for_emails(session_id: str, url: str, source_name: str, name: str = "", platform: str = "") -> int:
    """Fetch a URL and extract all emails from it. Returns count of new emails added."""
    count = 0
    try:
        resp = requests.get(url, headers=COMMON_HEADERS, timeout=12, allow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            emails = extract_emails_from_text(text)
            for e in emails:
                if add_email(session_id, e, source_name, name, platform):
                    count += 1
    except Exception:
        pass
    return count


def extract_links_from_serp(html: str, base_domain_blacklist: list) -> List[str]:
    """Parse search result HTML and extract result page URLs to scrape."""
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        if isinstance(a, Tag):
            href = a.get("href")
            if isinstance(href, list):
                href = href[0]
            if isinstance(href, str):
                # Google/Bing wrap URLs in redirect params
                if href.startswith("/url?q="):
                    href = href[7:].split("&")[0]
                if href.startswith("http") and not href.startswith("https://www.google") \
                        and not href.startswith("https://www.bing"):
                    parsed = urlparse(href)
                    domain = parsed.netloc.lower()
                    if not any(bl in domain for bl in base_domain_blacklist):
                        urls.append(href)
    return list(dict.fromkeys(urls))  # deduplicate while preserving order


@app.post("/api/search/start")
async def start_search(req: SearchRequest, background_tasks: BackgroundTasks):
    state = get_state(req.session_id)
    if state["running"]:
        raise HTTPException(400, "Search already running. Stop it first.")
    state["running"] = True
    state["emails"] = set()
    state["email_details"] = {}
    state["progress"] = 0
    state["log"] = []
    state["stats"] = {"github": 0, "google": 0, "bing": 0, "devfolio": 0, "gitlab": 0, "npm": 0, "pypi": 0, "devto": 0}
    state["search_count"] = state.get("search_count", 0) + 1  # increment run counter
    background_tasks.add_task(run_search, req)
    return {"status": "started"}


@app.get("/api/search/status")
async def get_status(session_id: str):
    state = get_state(session_id)
    return {
        "running": state["running"],
        "count": len(state["emails"]),
        "progress": state["progress"],
        "log": state["log"][-20:],
        "emails": list(state["email_details"].values()),
        "stats": state["stats"]
    }


@app.post("/api/search/stop")
async def stop_search(session_id: str):
    state = get_state(session_id)
    state["running"] = False
    log(session_id, "🛑 Search stopped by user.")
    return {"status": "stopped"}


async def run_search(req: SearchRequest):
    session_id = req.session_id
    state = get_state(session_id)
    try:
        tasks = []
        if "github" in req.sources:
            tasks.append(search_github(session_id, req.keyword, req.max_results))
        if "google" in req.sources:
            tasks.append(search_google(session_id, req.keyword))
        if "bing" in req.sources:
            tasks.append(search_bing(session_id, req.keyword))
        if "devfolio" in req.sources:
            tasks.append(search_devfolio(session_id, req.keyword))
        if "gitlab" in req.sources:
            tasks.append(search_gitlab(session_id, req.keyword, req.max_results))
        if "npm" in req.sources:
            tasks.append(search_npm(session_id, req.keyword))
        if "pypi" in req.sources:
            tasks.append(search_pypi(session_id, req.keyword))
        if "devto" in req.sources:
            tasks.append(search_devto(session_id, req.keyword))

        await asyncio.gather(*tasks)
        log(session_id, f"✅ Search complete! Found {len(state['emails'])} unique emails.")
    except Exception as e:
        log(session_id, f"❌ Fatal error: {e}")
    finally:
        state["running"] = False
        state["progress"] = 100


# ─── GITHUB ───────────────────────────────────────────────────────────────────

async def search_github(session_id: str, keyword: str, max_results: int):
    state = get_state(session_id)
    log(session_id, f"🐙 GitHub: Searching for '{keyword}' students...")
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "EmailExtractorTool/1.0"
    }
    if "GITHUB_TOKEN" in os.environ:
        headers["Authorization"] = f"token {os.environ['GITHUB_TOKEN']}"

    student_keywords = [
        keyword,
        f"{keyword} student",
        f"{keyword} developer",
        f"{keyword} university",
        f"{keyword} college",
    ]
    # Shuffle keywords so each run explores in a different order
    random.shuffle(student_keywords)

    seen_logins = set()
    # Offset starting page by run count so each new search fetches different users
    run_offset = state.get("search_count", 1) - 1
    page_start = (run_offset * 3) % 10 + 1  # cycles through pages 1-10 across runs

    for kw in student_keywords:
        if not state["running"] or len(state["emails"]) >= max_results:
            break
        # Build a rotated page range so we don't always start from page 1
        pages = list(range(page_start, 11)) + list(range(1, page_start))
        for page in pages:
            if not state["running"]:
                break
            try:
                url = f"https://api.github.com/search/users?q={quote(kw)}&per_page=30&page={page}"
                resp = requests.get(url, headers=headers, timeout=15)

                if resp.status_code == 403:
                    log(session_id, "⏳ GitHub rate limit hit. Waiting 60 seconds...")
                    await asyncio.sleep(60)
                    continue

                if resp.status_code != 200:
                    log(session_id, f"⚠️ GitHub returned status {resp.status_code}")
                    break

                data = resp.json()
                users = data.get("items", [])
                if not users:
                    break

                log(session_id, f"🐙 GitHub: Found {len(users)} profiles on page {page}...")

                for user in users:
                    if not state["running"]:
                        break
                    login = user["login"]
                    if login in seen_logins:
                        continue
                    seen_logins.add(login)

                    try:
                        found = await scrape_github_user(session_id, login, headers)
                        if found:
                            log(session_id, f"✅ GitHub [{login}]: {found} email(s) found")
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass

                await asyncio.sleep(1.5)

            except Exception as e:
                log(session_id, f"❌ GitHub error: {e}")
                break


async def scrape_github_user(session_id: str, login: str, headers: dict) -> int:
    """Fetch profile + README + repos for a GitHub user and extract emails without hitting API rate limits."""
    count = 0

    # 1. Profile README (fast, no rate limit)
    for branch in ["main", "master"]:
        try:
            readme_url = f"https://raw.githubusercontent.com/{login}/{login}/{branch}/README.md"
            r = requests.get(readme_url, timeout=8)
            if r.status_code == 200:
                for e in extract_emails_from_text(r.text):
                    if add_email(session_id, e, "github", login, f"github.com/{login} README"):
                        count += 1
                break
        except Exception:
            pass

    # 2. Scrape GitHub profile HTML directly (NO API rate limit!)
    try:
        r = requests.get(f"https://github.com/{login}", headers={"User-Agent": COMMON_HEADERS["User-Agent"]}, timeout=10)
        if r.status_code == 200:
            for e in extract_emails_from_text(r.text):
                if add_email(session_id, e, "github", login, f"github.com/{login} Profile"):
                    count += 1
            
            soup = BeautifulSoup(r.text, "html.parser")
            # Extract website link if present
            for a in soup.find_all("a", rel="nofollow me"):
                if isinstance(a, Tag):
                    href = a.get("href")
                    if isinstance(href, str) and href.startswith("http"):
                        n = scrape_page_for_emails(session_id, href, "github", login, f"github.com/{login} blog")
                        count += n

            # 3. Top repos from pinned items in HTML
            pinned = soup.find_all("span", class_="repo")
            for span in pinned:
                repo_name = span.text.strip()
                if repo_name:
                    for branch in ["main", "master"]:
                        try:
                            raw = f"https://raw.githubusercontent.com/{login}/{repo_name}/{branch}/README.md"
                            rr = requests.get(raw, timeout=8)
                            if rr.status_code == 200:
                                for e in extract_emails_from_text(rr.text):
                                    if add_email(session_id, e, "github", login, f"github.com/{login}/{repo_name}"):
                                        count += 1
                                break
                        except Exception:
                            pass
    except Exception:
        pass

    return count


# ─── GOOGLE ───────────────────────────────────────────────────────────────────

async def search_google(session_id: str, keyword: str):
    state = get_state(session_id)
    log(session_id, f"🔎 Google: Dorking for '{keyword}' student emails...")
    queries = [
        f'"{keyword}" student email contact site:github.io',
        f'"{keyword}" student "gmail.com" email portfolio',
        f'{keyword} student developer email resume site:github.io OR site:netlify.app',
        f'{keyword} "contact me" student email developer',
        f'{keyword} college student open source email',
    ]
    # Shuffle query order so each run explores different queries first
    random.shuffle(queries)

    scraped_urls = set()
    serp_blacklist = ["google.com", "youtube.com", "facebook.com", "twitter.com", "instagram.com"]
    # Vary the start parameter to get different result pages on each run
    run_offset = state.get("search_count", 1) - 1
    start_param = (run_offset * 10) % 50  # cycles 0, 10, 20, 30, 40

    for query in queries:
        if not state["running"]:
            break
        try:
            url = f"https://www.google.com/search?q={quote(query)}&num=30&start={start_param}"
            resp = requests.get(url, headers=COMMON_HEADERS, timeout=20)

            # Extract emails directly from SERP (unlikely but try)
            for e in extract_emails_from_text(resp.text):
                add_email(session_id, e, "google", "", "Google SERP")

            # Extract result URLs and scrape each
            result_urls = extract_links_from_serp(resp.text, serp_blacklist)
            log(session_id, f"🔎 Google: Found {len(result_urls)} pages to scrape for query...")

            for page_url in result_urls[:8]:  # scrape top 8 results per query
                if page_url in scraped_urls or not state["running"]:
                    continue
                scraped_urls.add(page_url)
                n = scrape_page_for_emails(session_id, page_url, "google", "", page_url)
                if n > 0:
                    log(session_id, f"✅ Google scraped: {n} email(s) from {page_url[:60]}")
                await asyncio.sleep(1)

            await asyncio.sleep(5)
        except Exception as e:
            log(session_id, f"❌ Google error: {e}")

    log(session_id, "🔎 Google dorking complete.")


# ─── BING ─────────────────────────────────────────────────────────────────────

async def search_bing(session_id: str, keyword: str):
    state = get_state(session_id)
    log(session_id, f"🅱️ Bing: Searching for '{keyword}' student emails...")
    queries = [
        f'"{keyword}" student email github.io portfolio',
        f'{keyword} college student developer email contact',
        f'{keyword} "contact" student email resume site:github.io',
    ]
    # Shuffle queries so each run tries a different query first
    random.shuffle(queries)

    scraped_urls = set()
    serp_blacklist = ["bing.com", "microsoft.com", "youtube.com", "facebook.com"]
    # Vary the first result offset on each run
    run_offset = state.get("search_count", 1) - 1
    first_param = (run_offset * 10) % 50  # cycles 0, 10, 20, 30, 40

    for query in queries:
        if not state["running"]:
            break
        try:
            url = f"https://www.bing.com/search?q={quote(query)}&count=30&first={first_param + 1}"
            resp = requests.get(url, headers=COMMON_HEADERS, timeout=20)

            # Direct SERP emails
            for e in extract_emails_from_text(resp.text):
                add_email(session_id, e, "bing", "", "Bing SERP")

            result_urls = extract_links_from_serp(resp.text, serp_blacklist)
            log(session_id, f"🅱️ Bing: Found {len(result_urls)} pages to scrape...")

            for page_url in result_urls[:8]:
                if page_url in scraped_urls or not state["running"]:
                    continue
                scraped_urls.add(page_url)
                n = scrape_page_for_emails(session_id, page_url, "bing", "", page_url)
                if n > 0:
                    log(session_id, f"✅ Bing scraped: {n} email(s) from {page_url[:60]}")
                await asyncio.sleep(1)

            await asyncio.sleep(3)
        except Exception as e:
            log(session_id, f"❌ Bing error: {e}")

    log(session_id, "🅱️ Bing search complete.")


# ─── DEVFOLIO ─────────────────────────────────────────────────────────────────

async def search_devfolio(session_id: str, keyword: str):
    log(session_id, f"🚀 Devfolio: Searching hackathon participants...")
    try:
        pages_to_scrape = [
            f"https://devfolio.co/search?q={quote(keyword)}",
            "https://devfolio.co/hackathons",
        ]

        try:
            api_resp = requests.get(
                "https://api.devfolio.co/api/hackathons?status=open&per_page=10",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=15
            )
            if api_resp.status_code == 200:
                hackathons = api_resp.json().get("results", [])
                for h in hackathons[:5]:
                    slug = h.get("slug", "")
                    if slug:
                        pages_to_scrape.append(f"https://devfolio.co/{slug}")
        except Exception:
            pass

        total = 0
        for url in pages_to_scrape:
            n = scrape_page_for_emails(session_id, url, "devfolio", "", "Devfolio")
            total += n
            await asyncio.sleep(2)

        log(session_id, f"🚀 Devfolio: Found {total} emails total")
    except Exception as e:
        log(session_id, f"❌ Devfolio error: {e}")


# ─── GITLAB ───────────────────────────────────────────────────────────────────

async def search_gitlab(session_id: str, keyword: str, max_results: int):
    state = get_state(session_id)
    log(session_id, f"🦊 GitLab: Searching for '{keyword}' users...")
    run_offset = state.get("search_count", 1) - 1
    page_start = (run_offset * 2) % 8 + 1

    search_terms = [keyword, f"{keyword} student", f"{keyword} developer"]
    random.shuffle(search_terms)
    seen_usernames: Set[str] = set()

    for term in search_terms:
        if not state["running"] or len(state["emails"]) >= max_results:
            break
        pages = list(range(page_start, 9)) + list(range(1, page_start))
        for page in pages:
            if not state["running"]:
                break
            try:
                url = f"https://gitlab.com/api/v4/users?search={quote(term)}&per_page=20&page={page}"
                resp = requests.get(url, headers=COMMON_HEADERS, timeout=15)
                if resp.status_code != 200:
                    break
                users = resp.json()
                if not users:
                    break
                log(session_id, f"🦊 GitLab: {len(users)} profiles on page {page}...")
                for user in users:
                    if not state["running"]:
                        break
                    username = user.get("username", "")
                    if not username or username in seen_usernames:
                        continue
                    seen_usernames.add(username)
                    # Scrape profile page HTML
                    try:
                        pr = requests.get(f"https://gitlab.com/{username}", headers=COMMON_HEADERS, timeout=10)
                        if pr.status_code == 200:
                            for e in extract_emails_from_text(pr.text):
                                if add_email(session_id, e, "gitlab", username, f"gitlab.com/{username}"):
                                    log(session_id, f"✅ GitLab [{username}]: email found")
                    except Exception:
                        pass
                    # Try profile README
                    for branch in ["main", "master"]:
                        try:
                            raw = f"https://gitlab.com/{username}/{username}/-/raw/{branch}/README.md"
                            rr = requests.get(raw, timeout=8)
                            if rr.status_code == 200:
                                for e in extract_emails_from_text(rr.text):
                                    add_email(session_id, e, "gitlab", username, f"gitlab.com/{username} README")
                                break
                        except Exception:
                            pass
                    await asyncio.sleep(0.4)
                await asyncio.sleep(1.5)
            except Exception as ex:
                log(session_id, f"❌ GitLab error: {ex}")
                break

    log(session_id, f"🦊 GitLab: Done. Total emails so far: {len(state['emails'])}")


# ─── NPM ──────────────────────────────────────────────────────────────────────

async def search_npm(session_id: str, keyword: str):
    state = get_state(session_id)
    log(session_id, f"📦 npm: Searching packages for '{keyword}'...")
    run_offset = state.get("search_count", 1) - 1
    # npm supports `from` for pagination
    from_offsets = [(run_offset * 50) % 200, 0, 50, 100, 150]

    seen_packages: Set[str] = set()
    total = 0

    for from_val in from_offsets:
        if not state["running"]:
            break
        try:
            url = f"https://registry.npmjs.org/-/v1/search?text={quote(keyword)}&size=50&from={from_val}"
            resp = requests.get(url, headers=COMMON_HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            packages = data.get("objects", [])
            if not packages:
                break
            log(session_id, f"📦 npm: {len(packages)} packages at offset {from_val}...")

            for obj in packages:
                if not state["running"]:
                    break
                pkg = obj.get("package", {})
                name = pkg.get("name", "")
                if not name or name in seen_packages:
                    continue
                seen_packages.add(name)

                # Extract from maintainers list (has email directly)
                for m in pkg.get("maintainers", []):
                    email = m.get("email", "")
                    if email and add_email(session_id, email, "npm", m.get("username", name), f"npmjs.com/package/{name}"):
                        total += 1

                # Fetch individual package JSON for author email
                try:
                    pkg_resp = requests.get(f"https://registry.npmjs.org/{name}/latest", timeout=10)
                    if pkg_resp.status_code == 200:
                        pkg_data = pkg_resp.json()
                        author = pkg_data.get("author", {})
                        if isinstance(author, dict):
                            email = author.get("email", "")
                            if email:
                                add_email(session_id, email, "npm", author.get("name", name), f"npmjs.com/package/{name}")
                        # Also check contributors
                        for contrib in pkg_data.get("contributors", []):
                            if isinstance(contrib, dict):
                                email = contrib.get("email", "")
                                if email:
                                    add_email(session_id, email, "npm", contrib.get("name", ""), f"npmjs.com/package/{name}")
                except Exception:
                    pass

            await asyncio.sleep(1)
        except Exception as ex:
            log(session_id, f"❌ npm error: {ex}")

    log(session_id, f"📦 npm: Done. Found {total} new emails.")


# ─── PYPI ─────────────────────────────────────────────────────────────────────

async def search_pypi(session_id: str, keyword: str):
    state = get_state(session_id)
    log(session_id, f"🐍 PyPI: Searching packages for '{keyword}'...")
    run_offset = state.get("search_count", 1) - 1
    page_start = (run_offset % 5) + 1
    pages = list(range(page_start, 6)) + list(range(1, page_start))

    seen_packages: Set[str] = set()
    total = 0

    for page in pages:
        if not state["running"]:
            break
        try:
            url = f"https://pypi.org/search/?q={quote(keyword)}&page={page}"
            resp = requests.get(url, headers=COMMON_HEADERS, timeout=15)
            if resp.status_code != 200:
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            # Package names are in <span class="package-snippet__name">
            pkg_names = [
                span.get_text(strip=True)
                for span in soup.find_all("span", class_="package-snippet__name")
            ]
            if not pkg_names:
                break
            log(session_id, f"🐍 PyPI: {len(pkg_names)} packages on page {page}...")

            for pkg_name in pkg_names:
                if not state["running"]:
                    break
                if pkg_name in seen_packages:
                    continue
                seen_packages.add(pkg_name)
                try:
                    pkg_resp = requests.get(f"https://pypi.org/pypi/{pkg_name}/json", timeout=10)
                    if pkg_resp.status_code == 200:
                        info = pkg_resp.json().get("info", {})
                        author_email = info.get("author_email", "") or ""
                        author_name = info.get("author", "") or pkg_name
                        # author_email can be comma-separated
                        for raw_email in author_email.replace(";", ",").split(","):
                            raw_email = raw_email.strip()
                            # Handle "Name <email>" format
                            match = re.search(r'<([^>]+)>', raw_email)
                            email = match.group(1) if match else raw_email
                            if email and add_email(session_id, email, "pypi", author_name, f"pypi.org/project/{pkg_name}"):
                                total += 1
                except Exception:
                    pass
                await asyncio.sleep(0.3)

            await asyncio.sleep(2)
        except Exception as ex:
            log(session_id, f"❌ PyPI error: {ex}")

    log(session_id, f"🐍 PyPI: Done. Found {total} new emails.")


# ─── DEV.TO ───────────────────────────────────────────────────────────────────

async def search_devto(session_id: str, keyword: str):
    state = get_state(session_id)
    log(session_id, f"👩‍💻 DEV.to: Searching articles for '{keyword}'...")
    run_offset = state.get("search_count", 1) - 1
    page_start = (run_offset % 5) + 1
    pages = list(range(page_start, 6)) + list(range(1, page_start))

    seen_users: Set[str] = set()
    total = 0

    for page in pages:
        if not state["running"]:
            break
        try:
            tag = quote(keyword.split()[0].lower())  # use first word as tag
            url = f"https://dev.to/api/articles?tag={tag}&per_page=30&page={page}"
            resp = requests.get(url, headers={**COMMON_HEADERS, "Accept": "application/json"}, timeout=15)
            if resp.status_code != 200:
                break
            articles = resp.json()
            if not articles:
                break
            log(session_id, f"👩‍💻 DEV.to: {len(articles)} articles on page {page}...")

            for article in articles:
                if not state["running"]:
                    break
                user = article.get("user", {})
                username = user.get("username", "")
                if not username or username in seen_users:
                    continue
                seen_users.add(username)

                # Scrape user profile page for email
                try:
                    profile_resp = requests.get(
                        f"https://dev.to/{username}",
                        headers=COMMON_HEADERS, timeout=10
                    )
                    if profile_resp.status_code == 200:
                        for e in extract_emails_from_text(profile_resp.text):
                            if add_email(session_id, e, "devto", user.get("name", username), f"dev.to/{username}"):
                                total += 1
                except Exception:
                    pass

                # Also scan article body for emails
                article_url = article.get("url", "")
                if article_url:
                    try:
                        art_resp = requests.get(article_url, headers=COMMON_HEADERS, timeout=10)
                        if art_resp.status_code == 200:
                            for e in extract_emails_from_text(art_resp.text):
                                add_email(session_id, e, "devto", user.get("name", username), article_url)
                    except Exception:
                        pass

                await asyncio.sleep(0.5)

            await asyncio.sleep(2)
        except Exception as ex:
            log(session_id, f"❌ DEV.to error: {ex}")

    log(session_id, f"👩‍💻 DEV.to: Done. Found {total} new emails.")


# ─── CAMPAIGN ─────────────────────────────────────────────────────────────────

def send_email_sync(smtp_server: str, smtp_port: int, sender_email: str, sender_password: str, to_email: str, subject: str, body_html: str, attachments: list):
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body_html, 'html'))
    
    for attachment in attachments:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(attachment['content'])
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{attachment["filename"]}"')
        msg.attach(part)
    
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)

def send_email_sendgrid_sync(api_key: str, sender_email: str, to_email: str, subject: str, body_html: str, attachments: list):
    sg = sendgrid.SendGridAPIClient(api_key=api_key)
    message = Mail(
        from_email=sender_email,
        to_emails=to_email,
        subject=subject,
        html_content=body_html
    )
    
    for att in attachments:
        encoded_content = base64.b64encode(att['content']).decode()
        attachment = Attachment(
            FileContent(encoded_content),
            FileName(att["filename"]),
            FileType('application/octet-stream'),
            Disposition('attachment')
        )
        message.attachment = attachment

    response = sg.send(message)
    if response.status_code >= 400:
        raise Exception(f"SendGrid Error {response.status_code}")

@app.post("/api/campaign/start")
async def start_campaign(
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    send_method: str = Form("smtp"),
    sg_api_key: str = Form(""),
    sg_sender: str = Form(""),
    smtp_server: str = Form(""),
    smtp_port: int = Form(587),
    senders_raw: str = Form("[]"),
    manual_emails: str = Form(""),
    subject: str = Form(...),
    body: str = Form(...),
    min_delay: int = Form(...),
    max_delay: int = Form(...),
    files: List[UploadFile] = File(default=[])
):
    state = get_campaign_state(session_id)
    if state["running"]:
        raise HTTPException(400, "Campaign already running. Stop it first.")
    
    search_state = get_state(session_id)
    target_emails = list(search_state.get("email_details", {}).values())
    
    if manual_emails.strip():
        for line in manual_emails.replace(",", "\n").split("\n"):
            email = line.strip()
            if email and "@" in email:
                target_emails.append({
                    "email": email,
                    "name": "",
                    "source": "Manual",
                    "platform": "Manual"
                })
    
    if not target_emails:
        raise HTTPException(400, "No extracted or manual emails to send to. Please search or add manual emails.")
        
    try:
        senders = json.loads(senders_raw)
        if send_method == "smtp" and not senders:
            raise ValueError()
    except Exception:
        if send_method == "smtp":
            raise HTTPException(400, "Please provide at least one sender account in valid JSON format.")

    if send_method == "sendgrid":
        if not sg_api_key or not sg_sender:
            raise HTTPException(400, "Please provide SendGrid API Key and Sender Email.")

    state["running"] = True
    state["sent"] = 0
    state["failed"] = 0
    state["total"] = len(target_emails)
    state["progress"] = 0
    state["log"] = []
    
    # Read files into memory so we can attach them later without file stream issues
    attachments = []
    for file in files:
        if file.filename:
            content = await file.read()
            attachments.append({
                "filename": file.filename,
                "content": content
            })
            
    # We construct a CampaignRequest-like object internally
    req_dict = {
        "session_id": session_id,
        "send_method": send_method,
        "sg_api_key": sg_api_key,
        "sg_sender": sg_sender,
        "smtp_server": smtp_server,
        "smtp_port": smtp_port,
        "senders": [SMTPSender(**s) for s in senders] if senders else [],
        "subject": subject,
        "body": body,
        "min_delay": min_delay,
        "max_delay": max_delay
    }
    
    req_obj = CampaignRequest(**req_dict)
    
    background_tasks.add_task(run_campaign, req_obj, target_emails, attachments)
    return {"status": "started"}

@app.get("/api/campaign/status")
async def get_campaign_status(session_id: str):
    return get_campaign_state(session_id)

@app.post("/api/campaign/stop")
async def stop_campaign(session_id: str):
    state = get_campaign_state(session_id)
    state["running"] = False
    log_campaign(session_id, "🛑 Campaign stopped by user.")
    return {"status": "stopped"}

async def run_campaign(req: CampaignRequest, targets: list, attachments: list):
    session_id = req.session_id
    state = get_campaign_state(session_id)
    
    log_campaign(session_id, f"🚀 Campaign started for {len(targets)} targets.")
    log_campaign(session_id, f"🕒 Using delay between {req.min_delay}s and {req.max_delay}s.")
    
    sender_idx = 0
    
    for i, target in enumerate(targets):
        if not state["running"]:
            break
            
        target_email = target["email"]
        target_name = target.get("name", "") or "Student"
        target_source = target.get("source", "")
        
        # Round robin sender
        sender = req.senders[sender_idx % len(req.senders)]
        sender_idx += 1
        
        # Replace variables in body/subject
        subject = req.subject.replace("{email}", target_email).replace("{name}", target_name).replace("{source}", target_source)
        body = req.body.replace("{email}", target_email).replace("{name}", target_name).replace("{source}", target_source)
        
        # Send email via thread to avoid blocking event loop
        log_campaign(session_id, f"📧 Sending to {target_email}...")
        try:
            if req.send_method == "sendgrid":
                await asyncio.to_thread(
                    send_email_sendgrid_sync,
                    req.sg_api_key,
                    req.sg_sender,
                    target_email,
                    subject,
                    body,
                    attachments
                )
            else:
                await asyncio.to_thread(
                    send_email_sync,
                    req.smtp_server, req.smtp_port,
                    sender.email, sender.password,
                    target_email, subject, body,
                    attachments
                )
            state["sent"] += 1
            log_campaign(session_id, f"✅ Sent to {target_email}")
        except Exception as e:
            state["failed"] += 1
            log_campaign(session_id, f"❌ Failed to {target_email}: {str(e)}")
            
        state["progress"] = int(((i + 1) / len(targets)) * 100)
        
        # Delay (if not last item)
        if i < len(targets) - 1 and state["running"]:
            delay = random.randint(req.min_delay, req.max_delay)
            log_campaign(session_id, f"⏳ Waiting {delay} seconds before next email...")
            await asyncio.sleep(delay)
            
    state["running"] = False
    if state["progress"] < 100:
        log_campaign(session_id, "🛑 Campaign halted.")
    else:
        state["progress"] = 100
        log_campaign(session_id, "✅ Campaign finished!")


# Serve static files (frontend)
app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    print(f"\n[*] Student Email Extractor starting...")
    print(f"[*] Open your browser at: http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
