import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

CONFIG_PATH = Path("config.json")
SEEN_PATH = Path("seen_jobs.json")
TIMEOUT_SECONDS = 20
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,ko;q=0.8,en;q=0.7",
}


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def contains_any(text: str, words: List[str]) -> bool:
    lower = text.lower()
    return any(w.lower() in lower for w in words)


def fetch_text(url: str) -> str:
    print(f"Fetching: {url}", flush=True)
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.text


def extract_candidates(company: str, url: str, html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    text = normalize(soup.get_text(" "))

    # Simple robust extraction: collect links that look like job links; if not enough, treat page as one candidate.
    candidates = []
    for a in soup.find_all("a", href=True):
        title = normalize(a.get_text(" "))
        href = a["href"]
        if not title or len(title) < 2:
            continue
        if any(x in href.lower() for x in ["job", "position", "career", "recruit", "search"]):
            if href.startswith("/"):
                from urllib.parse import urljoin
                href = urljoin(url, href)
            candidates.append({"company": company, "title": title[:120], "url": href, "content": title})

    if not candidates:
        candidates.append({"company": company, "title": f"{company} 채용 페이지", "url": url, "content": text[:5000]})
    else:
        # add page text to each candidate for keyword detection in pages that render lists as text
        for c in candidates:
            c["content"] = f'{c["title"]} {text[:3000]}'

    # dedupe
    seen = set()
    unique = []
    for c in candidates:
        key = c["url"] + "|" + c["title"]
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique[:80]


def filter_jobs(candidates: List[Dict[str, str]], rules: Dict) -> List[Dict[str, str]]:
    must = rules["must_include_korea_keywords"]
    exclude = rules.get("exclude_keywords", [])
    matched = []
    for c in candidates:
        hay = f'{c.get("title", "")} {c.get("content", "")}'
        if not contains_any(hay, must):
            continue
        if contains_any(hay, exclude):
            continue
        c["matched_keywords"] = ", ".join([w for w in must if w.lower() in hay.lower()])
        matched.append(c)
    return matched


def send_email(to_addr: str, subject: str, body: str):
    user = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASSWORD")
    if not user or not password:
        print("EMAIL_USER / EMAIL_PASSWORD not set. Test mode: email not sent.", flush=True)
        print(subject, flush=True)
        print(body[:1000], flush=True)
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, [to_addr], msg.as_string())
    print(f"Email sent to {to_addr}", flush=True)


def main():
    config = load_json(CONFIG_PATH, {})
    if not config:
        print("config.json not found or invalid", flush=True)
        sys.exit(1)

    seen = load_json(SEEN_PATH, {})
    rules = config["rules"]
    to_addr = config["email"]["to"]
    new_matches = []

    for company in config["companies"]:
        name = company["name"]
        if not company.get("enabled", True):
            print(f"Skip disabled company: {name}", flush=True)
            continue
        print(f"Checking company: {name}", flush=True)
        for url in company["urls"]:
            try:
                html = fetch_text(url)
                candidates = extract_candidates(name, url, html)
                print(f"Extracted {len(candidates)} candidates from {name}", flush=True)
                matches = filter_jobs(candidates, rules)
                print(f"Matched {len(matches)} Korea-related jobs from {name}", flush=True)
                for job in matches:
                    job_id = f'{name}|{job["url"]}|{job["title"]}'
                    if job_id in seen:
                        continue
                    seen[job_id] = True
                    new_matches.append(job)
            except Exception as e:
                print(f"ERROR while checking {name} / {url}: {type(e).__name__}: {e}", flush=True)
                continue

    save_json(SEEN_PATH, seen)

    if not new_matches:
        print("No new matched jobs.", flush=True)
        return

    lines = []
    for j in new_matches:
        lines.append(f"회사: {j['company']}\n공고/페이지: {j['title']}\n매칭 키워드: {j.get('matched_keywords','')}\n링크: {j['url']}\n")
    body = "한국 관련 키워드가 포함된 신규 공고를 발견했습니다.\n\n" + "\n---\n".join(lines)
    send_email(to_addr, f"[채용공고 알림] 신규 한국 관련 공고 {len(new_matches)}건", body)


if __name__ == "__main__":
    main()
