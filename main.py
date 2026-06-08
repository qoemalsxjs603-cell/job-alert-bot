import asyncio
import hashlib
import json
import os
import re
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Dict, Iterable, List, Set
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

CONFIG_PATH = Path("config.json")
SEEN_PATH = Path("data/seen_jobs.json")
MAX_LINKS_PER_SOURCE = 20
PAGE_TIMEOUT_MS = 20000
DETAIL_TIMEOUT_SECONDS = 12
SOURCE_TIMEOUT_SECONDS = 45
COMPANY_TIMEOUT_SECONDS = 120


@dataclass
class JobMatch:
    company: str
    title: str
    url: str
    source_url: str
    matched_korea_keywords: List[str]
    matched_optional_keywords: List[str]


def load_config() -> Dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_seen() -> Set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        with SEEN_PATH.open("r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: Set[str]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SEEN_PATH.open("w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def contains_any(text: str, keywords: Iterable[str]) -> List[str]:
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]


def make_job_id(company: str, title: str, url: str) -> str:
    raw = f"{company}|{title}|{url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_probably_job_link(href: str, text: str) -> bool:
    href_lower = (href or "").lower()
    text = normalize_space(text)
    if not href or href.startswith("javascript:") or href.startswith("mailto:"):
        return False
    patterns = ["job", "position", "career", "recruit", "zhipin.com/job_detail", "zhipin.com/gongsi/job"]
    return any(p in href_lower for p in patterns) or len(text) >= 4


async def fetch_html(page, url: str, timeout_ms: int = PAGE_TIMEOUT_MS) -> str:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1500)
        return await page.content()
    except PlaywrightTimeoutError:
        print(f"  - Timeout: {url}", flush=True)
        return ""
    except Exception as e:
        print(f"  - Load failed: {url} / {type(e).__name__}: {e}", flush=True)
        return ""


def extract_links(html: str, base_url: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    links = []
    seen_urls = set()
    for a in soup.find_all("a"):
        href = a.get("href")
        text = normalize_space(a.get_text(" "))
        if not href:
            continue
        full_url = urljoin(base_url, href).split("#")[0]
        if full_url in seen_urls:
            continue
        if is_probably_job_link(full_url, text):
            seen_urls.add(full_url)
            links.append({"title": text[:120] or "채용공고", "url": full_url})
    return links[:MAX_LINKS_PER_SOURCE]


def page_title_and_text(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    title = normalize_space(soup.title.get_text(" ") if soup.title else "")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = normalize_space(soup.get_text(" "))
    return {"title": title, "text": text}


async def check_source(page, company: Dict, source_url: str, rules: Dict, seen: Set[str]) -> List[JobMatch]:
    print(f"Checking {company['name']}: {source_url}", flush=True)
    matches: List[JobMatch] = []
    html = await fetch_html(page, source_url)
    if not html:
        print(f"  - Skipped source because it could not be loaded.", flush=True)
        return matches

    source_info = page_title_and_text(html)
    candidates = [{"title": source_info["title"] or company["name"], "url": source_url}]
    candidates.extend(extract_links(html, source_url))

    unique_candidates = []
    candidate_urls = set()
    source_host = urlparse(source_url).netloc
    for c in candidates:
        job_url = c["url"]
        if job_url in candidate_urls:
            continue
        if urlparse(job_url).netloc and source_host and urlparse(job_url).netloc != source_host:
            continue
        unique_candidates.append(c)
        candidate_urls.add(job_url)

    print(f"  - Candidate links: {len(unique_candidates)}", flush=True)

    for c in unique_candidates[:MAX_LINKS_PER_SOURCE]:
        job_url = c["url"]
        job_title = c["title"]
        try:
            detail_html = html if job_url == source_url else await asyncio.wait_for(
                fetch_html(page, job_url, timeout_ms=10000),
                timeout=DETAIL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            print(f"  - Detail timeout: {job_url}", flush=True)
            continue

        if not detail_html:
            continue

        info = page_title_and_text(detail_html)
        title = normalize_space(job_title or info["title"] or company["name"])
        searchable_text = f"{title} {info['title']} {info['text']} {job_url}"

        korea_hits = contains_any(searchable_text, rules["must_include_korea_keywords"])
        if not korea_hits:
            continue

        exclude_hits = contains_any(searchable_text, rules["exclude_keywords"])
        if exclude_hits:
            print(f"  - Excluded: {title} / {exclude_hits}", flush=True)
            continue

        job_id = make_job_id(company["name"], title, job_url)
        if job_id in seen:
            continue

        optional_hits = contains_any(searchable_text, rules["optional_keywords"])
        matches.append(JobMatch(company["name"], title[:160], job_url, source_url, korea_hits, optional_hits))
        seen.add(job_id)
        print(f"  - Matched: {title}", flush=True)

    return matches


async def collect_matches_for_company(browser, company: Dict, rules: Dict, seen: Set[str]) -> List[JobMatch]:
    matches: List[JobMatch] = []
    context = await browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        locale="zh-CN",
        viewport={"width": 1440, "height": 1200},
    )
    page = await context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)

    try:
        for source_url in company["urls"]:
            try:
                source_matches = await asyncio.wait_for(
                    check_source(page, company, source_url, rules, seen),
                    timeout=SOURCE_TIMEOUT_SECONDS,
                )
                matches.extend(source_matches)
            except asyncio.TimeoutError:
                print(f"  - Source skipped by timeout: {source_url}", flush=True)
            except Exception as e:
                print(f"  - Source error: {source_url} / {type(e).__name__}: {e}", flush=True)
    finally:
        await context.close()
    return matches


def build_email_body(matches: List[JobMatch]) -> str:
    lines = ["한국 관련 키워드가 포함된 신규 채용공고가 감지되었습니다.", ""]
    for idx, m in enumerate(matches, 1):
        lines += [
            f"[{idx}] {m.company}",
            f"공고명: {m.title}",
            f"링크: {m.url}",
            f"감지된 한국 키워드: {', '.join(m.matched_korea_keywords)}",
        ]
        if m.matched_optional_keywords:
            lines.append(f"보조 키워드: {', '.join(m.matched_optional_keywords)}")
        lines += [f"수집 출처: {m.source_url}", ""]
    lines += ["--", "Job Alert Bot"]
    return "\n".join(lines)


def send_email(to_email: str, matches: List[JobMatch]) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER") or os.getenv("EMAIL_USER")
    smtp_password = os.getenv("SMTP_PASSWORD") or os.getenv("EMAIL_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM") or smtp_user or ""

    if not all([smtp_host, smtp_user, smtp_password, smtp_from]):
        print("Email secrets are not set. Test mode: email will not be sent.", flush=True)
        print(build_email_body(matches), flush=True)
        return

    msg = MIMEText(build_email_body(matches), "plain", "utf-8")
    msg["Subject"] = f"[채용공고 알림] 한국 관련 신규 공고 {len(matches)}건"
    msg["From"] = formataddr(("Job Alert Bot", smtp_from))
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_from, [to_email], msg.as_string())


async def main() -> None:
    config = load_config()
    seen = load_seen()
    all_matches: List[JobMatch] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for company in config["companies"]:
                try:
                    matches = await asyncio.wait_for(
                        collect_matches_for_company(browser, company, config["rules"], seen),
                        timeout=COMPANY_TIMEOUT_SECONDS,
                    )
                    all_matches.extend(matches)
                except asyncio.TimeoutError:
                    print(f"Company skipped by timeout: {company['name']}", flush=True)
        finally:
            await browser.close()

    save_seen(seen)
    if all_matches:
        print(f"Found {len(all_matches)} new matching jobs.", flush=True)
        send_email(config["email"]["to"], all_matches)
    else:
        print("No new matching jobs found.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
