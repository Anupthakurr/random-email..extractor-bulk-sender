import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import re
import asyncio
import os
from typing import List, Set, Dict, TypedDict
from urllib.parse import urlparse, urljoin, quote
from bs4 import BeautifulSoup, Tag

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

search_state: SearchState = {
    "running": False,
    "emails": set(),
    "email_details": {},
    "progress": 0,
    "log": [],
    "stats": {"github": 0, "google": 0, "bing": 0, "devfolio": 0}
}

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class SearchRequest(BaseModel):
    keyword: str
    sources: List[str]
    max_results: int = 500


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
    return True


def add_email(email: str, source: str, name: str = "", platform: str = "") -> bool:
    email = email.lower().strip()
    if is_valid_email(email) and email not in search_state["emails"]:
        search_state["emails"].add(email)
        search_state["email_details"][email] = {
            "email": email,
            "source": source,
            "name": name,
            "platform": platform
        }
        search_state["stats"][source] = search_state["stats"].get(source, 0) + 1
        return True
    return False


def log(msg: str):
    search_state["log"].append(msg)
    if len(search_state["log"]) > 200:
        search_state["log"] = search_state["log"][-200:]
    print(msg)


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


def scrape_page_for_emails(url: str, source_name: str, name: str = "", platform: str = "") -> int:
    """Fetch a URL and extract all emails from it. Returns count of new emails added."""
    count = 0
    try:
        resp = requests.get(url, headers=COMMON_HEADERS, timeout=12, allow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            emails = extract_emails_from_text(text)
            for e in emails:
                if add_email(e, source_name, name, platform):
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
    if search_state["running"]:
        raise HTTPException(400, "Search already running. Stop it first.")
    search_state["running"] = True
    search_state["emails"] = set()
    search_state["email_details"] = {}
    search_state["progress"] = 0
    search_state["log"] = []
    search_state["stats"] = {"github": 0, "google": 0, "bing": 0, "devfolio": 0}
    background_tasks.add_task(run_search, req)
    return {"status": "started"}


@app.get("/api/search/status")
async def get_status():
    return {
        "running": search_state["running"],
        "count": len(search_state["emails"]),
        "progress": search_state["progress"],
        "log": search_state["log"][-20:],
        "emails": list(search_state["email_details"].values()),
        "stats": search_state["stats"]
    }


@app.post("/api/search/stop")
async def stop_search():
    search_state["running"] = False
    log("🛑 Search stopped by user.")
    return {"status": "stopped"}


async def run_search(req: SearchRequest):
    try:
        tasks = []
        if "github" in req.sources:
            tasks.append(search_github(req.keyword, req.max_results))
        if "google" in req.sources:
            tasks.append(search_google(req.keyword))
        if "bing" in req.sources:
            tasks.append(search_bing(req.keyword))
        if "devfolio" in req.sources:
            tasks.append(search_devfolio(req.keyword))

        await asyncio.gather(*tasks)
        log(f"✅ Search complete! Found {len(search_state['emails'])} unique emails.")
    except Exception as e:
        log(f"❌ Fatal error: {e}")
    finally:
        search_state["running"] = False
        search_state["progress"] = 100


# ─── GITHUB ───────────────────────────────────────────────────────────────────

async def search_github(keyword: str, max_results: int):
    log(f"🐙 GitHub: Searching for '{keyword}' students...")
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

    seen_logins = set()

    for kw in student_keywords:
        if not search_state["running"] or len(search_state["emails"]) >= max_results:
            break
        for page in range(1, 11):
            if not search_state["running"]:
                break
            try:
                url = f"https://api.github.com/search/users?q={quote(kw)}&per_page=30&page={page}"
                resp = requests.get(url, headers=headers, timeout=15)

                if resp.status_code == 403:
                    log("⏳ GitHub rate limit hit. Waiting 60 seconds...")
                    await asyncio.sleep(60)
                    continue

                if resp.status_code != 200:
                    log(f"⚠️ GitHub returned status {resp.status_code}")
                    break

                data = resp.json()
                users = data.get("items", [])
                if not users:
                    break

                log(f"🐙 GitHub: Found {len(users)} profiles on page {page}...")

                for user in users:
                    if not search_state["running"]:
                        break
                    login = user["login"]
                    if login in seen_logins:
                        continue
                    seen_logins.add(login)

                    try:
                        found = await scrape_github_user(login, headers)
                        if found:
                            log(f"✅ GitHub [{login}]: {found} email(s) found")
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass

                await asyncio.sleep(1.5)

            except Exception as e:
                log(f"❌ GitHub error: {e}")
                break


async def scrape_github_user(login: str, headers: dict) -> int:
    """Fetch profile + README + repos for a GitHub user and extract emails without hitting API rate limits."""
    count = 0

    # 1. Profile README (fast, no rate limit)
    for branch in ["main", "master"]:
        try:
            readme_url = f"https://raw.githubusercontent.com/{login}/{login}/{branch}/README.md"
            r = requests.get(readme_url, timeout=8)
            if r.status_code == 200:
                for e in extract_emails_from_text(r.text):
                    if add_email(e, "github", login, f"github.com/{login} README"):
                        count += 1
                break
        except Exception:
            pass

    # 2. Scrape GitHub profile HTML directly (NO API rate limit!)
    try:
        r = requests.get(f"https://github.com/{login}", headers={"User-Agent": COMMON_HEADERS["User-Agent"]}, timeout=10)
        if r.status_code == 200:
            for e in extract_emails_from_text(r.text):
                if add_email(e, "github", login, f"github.com/{login} Profile"):
                    count += 1
            
            soup = BeautifulSoup(r.text, "html.parser")
            # Extract website link if present
            for a in soup.find_all("a", rel="nofollow me"):
                href = a.get("href")
                if isinstance(href, str) and href.startswith("http"):
                    n = scrape_page_for_emails(href, "github", login, f"github.com/{login} blog")
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
                                    if add_email(e, "github", login, f"github.com/{login}/{repo_name}"):
                                        count += 1
                                break
                        except Exception:
                            pass
    except Exception:
        pass

    return count


# ─── GOOGLE ───────────────────────────────────────────────────────────────────

async def search_google(keyword: str):
    log(f"🔎 Google: Dorking for '{keyword}' student emails...")
    queries = [
        f'"{keyword}" student email contact site:github.io',
        f'"{keyword}" student "gmail.com" email portfolio',
        f'{keyword} student developer email resume site:github.io OR site:netlify.app',
        f'{keyword} "contact me" student email developer',
        f'{keyword} college student open source email',
    ]

    scraped_urls = set()
    serp_blacklist = ["google.com", "youtube.com", "facebook.com", "twitter.com", "instagram.com"]

    for query in queries:
        if not search_state["running"]:
            break
        try:
            url = f"https://www.google.com/search?q={quote(query)}&num=30"
            resp = requests.get(url, headers=COMMON_HEADERS, timeout=20)

            # Extract emails directly from SERP (unlikely but try)
            for e in extract_emails_from_text(resp.text):
                add_email(e, "google", "", "Google SERP")

            # Extract result URLs and scrape each
            result_urls = extract_links_from_serp(resp.text, serp_blacklist)
            log(f"🔎 Google: Found {len(result_urls)} pages to scrape for query...")

            for page_url in result_urls[:8]:  # scrape top 8 results per query
                if page_url in scraped_urls or not search_state["running"]:
                    continue
                scraped_urls.add(page_url)
                n = scrape_page_for_emails(page_url, "google", "", page_url)
                if n > 0:
                    log(f"✅ Google scraped: {n} email(s) from {page_url[:60]}")
                await asyncio.sleep(1)

            await asyncio.sleep(5)
        except Exception as e:
            log(f"❌ Google error: {e}")

    log("🔎 Google dorking complete.")


# ─── BING ─────────────────────────────────────────────────────────────────────

async def search_bing(keyword: str):
    log(f"🅱️ Bing: Searching for '{keyword}' student emails...")
    queries = [
        f'"{keyword}" student email github.io portfolio',
        f'{keyword} college student developer email contact',
        f'{keyword} "contact" student email resume site:github.io',
    ]

    scraped_urls = set()
    serp_blacklist = ["bing.com", "microsoft.com", "youtube.com", "facebook.com"]

    for query in queries:
        if not search_state["running"]:
            break
        try:
            url = f"https://www.bing.com/search?q={quote(query)}&count=30"
            resp = requests.get(url, headers=COMMON_HEADERS, timeout=20)

            # Direct SERP emails
            for e in extract_emails_from_text(resp.text):
                add_email(e, "bing", "", "Bing SERP")

            result_urls = extract_links_from_serp(resp.text, serp_blacklist)
            log(f"🅱️ Bing: Found {len(result_urls)} pages to scrape...")

            for page_url in result_urls[:8]:
                if page_url in scraped_urls or not search_state["running"]:
                    continue
                scraped_urls.add(page_url)
                n = scrape_page_for_emails(page_url, "bing", "", page_url)
                if n > 0:
                    log(f"✅ Bing scraped: {n} email(s) from {page_url[:60]}")
                await asyncio.sleep(1)

            await asyncio.sleep(3)
        except Exception as e:
            log(f"❌ Bing error: {e}")

    log("🅱️ Bing search complete.")


# ─── DEVFOLIO ─────────────────────────────────────────────────────────────────

async def search_devfolio(keyword: str):
    log(f"🚀 Devfolio: Searching hackathon participants...")
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
            n = scrape_page_for_emails(url, "devfolio", "", "Devfolio")
            total += n
            await asyncio.sleep(2)

        log(f"🚀 Devfolio: Found {total} emails total")
    except Exception as e:
        log(f"❌ Devfolio error: {e}")


# Serve static files (frontend)
app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("\n[*] Student Email Extractor starting...")
    print("[*] Open your browser at: http://localhost:8001\n")
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
