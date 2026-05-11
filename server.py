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
from typing import List

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
    "babel.io", "eslint.org", "schema.org"
]

search_state = {
    "running": False,
    "emails": set(),
    "email_details": {},
    "progress": 0,
    "log": [],
    "stats": {"github": 0, "google": 0, "bing": 0, "devfolio": 0}
}


class SearchRequest(BaseModel):
    keyword: str
    sources: List[str]
    max_results: int = 500


def is_valid_email(email: str) -> bool:
    email = email.lower()
    if any(b in email for b in BLOCKED_DOMAINS):
        return False
    if email.endswith(".png") or email.endswith(".jpg") or email.endswith(".svg"):
        return False
    if "your-email" in email or "example" in email:
        return False
    parts = email.split("@")
    if len(parts) != 2 or len(parts[0]) < 2:
        return False
    return True


def add_email(email: str, source: str, name: str = "", platform: str = ""):
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


def log(msg: str):
    search_state["log"].append(msg)
    if len(search_state["log"]) > 100:
        search_state["log"] = search_state["log"][-100:]
    print(msg)


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


async def search_github(keyword: str, max_results: int):
    log(f"🐙 GitHub: Searching for '{keyword}' students...")
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "EmailExtractorTool/1.0"
    }

    student_keywords = [keyword, f"{keyword} student", f"{keyword} college", f"{keyword} university"]

    for kw in student_keywords:
        if not search_state["running"] or len(search_state["emails"]) >= max_results:
            break
        for page in range(1, 6):
            if not search_state["running"]:
                break
            try:
                url = f"https://api.github.com/search/users?q={requests.utils.quote(kw)}&per_page=30&page={page}"
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
                    try:
                        profile_url = f"https://api.github.com/users/{user['login']}"
                        profile_resp = requests.get(profile_url, headers=headers, timeout=10)

                        if profile_resp.status_code == 200:
                            profile = profile_resp.json()
                            email = profile.get("email")
                            name = profile.get("name") or user["login"]
                            bio = profile.get("bio") or ""

                            if email:
                                add_email(email, "github", name, f"github.com/{user['login']}")
                                log(f"✅ GitHub: {email} ({name})")

                            # Extract from bio
                            bio_emails = EMAIL_REGEX.findall(bio)
                            for e in bio_emails:
                                add_email(e, "github", name, f"github.com/{user['login']}")
                                log(f"✅ GitHub Bio: {e}")

                        await asyncio.sleep(0.8)
                    except Exception:
                        pass

                await asyncio.sleep(1.5)

            except Exception as e:
                log(f"❌ GitHub error: {e}")
                break


async def search_google(keyword: str):
    log(f"🔎 Google: Dorking for '{keyword}' student emails...")
    queries = [
        f'{keyword} student email "@gmail.com" site:github.com',
        f'{keyword} "computer science" student email contact',
        f'{keyword} student "@college.edu" email',
        f'"{keyword}" student email site:devfolio.co',
        f'"{keyword}" engineering student email contact site:linkedin.com',
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/"
    }

    for query in queries:
        if not search_state["running"]:
            break
        try:
            url = f"https://www.google.com/search?q={requests.utils.quote(query)}&num=50"
            resp = requests.get(url, headers=headers, timeout=20)
            emails = EMAIL_REGEX.findall(resp.text)
            new_count = 0
            for e in emails:
                if add_email(e, "google", "", "Google Search") is None:
                    new_count += 1
            if emails:
                log(f"🔎 Google: Found {len(emails)} emails from query")
            await asyncio.sleep(5)
        except Exception as e:
            log(f"❌ Google error: {e}")

    log("🔎 Google dorking complete.")


async def search_bing(keyword: str):
    log(f"🅱️ Bing: Searching for '{keyword}' student emails...")
    queries = [
        f'{keyword} student email "@gmail.com"',
        f'{keyword} college student contact email',
        f'"{keyword}" student "@edu" email',
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html"
    }

    for query in queries:
        if not search_state["running"]:
            break
        try:
            url = f"https://www.bing.com/search?q={requests.utils.quote(query)}&count=50"
            resp = requests.get(url, headers=headers, timeout=20)
            emails = EMAIL_REGEX.findall(resp.text)
            for e in emails:
                add_email(e, "bing", "", "Bing Search")
            if emails:
                log(f"🅱️ Bing: Found {len(emails)} emails from query")
            await asyncio.sleep(3)
        except Exception as e:
            log(f"❌ Bing error: {e}")

    log("🅱️ Bing search complete.")


async def search_devfolio(keyword: str):
    log(f"🚀 Devfolio: Searching hackathon participants...")
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }
    try:
        # Search hackathons
        url = "https://api.devfolio.co/api/hackathons?status=open&per_page=20"
        resp = requests.get(url, headers=headers, timeout=15)
        emails = EMAIL_REGEX.findall(resp.text)
        for e in emails:
            add_email(e, "devfolio", "", "Devfolio")
        log(f"🚀 Devfolio: Extracted {len(emails)} emails from hackathon listings")

        # Try public project pages
        url2 = f"https://devfolio.co/search?q={requests.utils.quote(keyword)}"
        resp2 = requests.get(url2, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        emails2 = EMAIL_REGEX.findall(resp2.text)
        for e in emails2:
            add_email(e, "devfolio", "", "Devfolio Projects")
        log(f"🚀 Devfolio projects: Found {len(emails2)} additional emails")

    except Exception as e:
        log(f"❌ Devfolio error: {e}")


# Serve static files (frontend)
app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("\n[*] Student Email Extractor starting...")
    print("[*] Open your browser at: http://localhost:8000\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
