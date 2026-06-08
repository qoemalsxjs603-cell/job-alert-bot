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
MAX_LINKS_PER_SOURCE = 60
PAGE_TIMEOUT_MS = 45000


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
    matched = []
    for kw in keywords:
        if kw.lower() in text_lower:
            matched.append(kw)
    return matched


def make_job_id(company: str, title: str, url: str) -> str:
    raw = f"{company}|{title}|{url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_probably_job_link(href: str, text: str) -> bool:
    href_lower = (href or "").lower()
    text = normalize_space(text)
    if not href or href.startswith("javascript:") or href.startswith("mailto:"):
        return False
    if len(text) < 2 and "job" not in href_lower:
        return False
    patterns = ["job", "position", "career", "recruit", "zhipin.com/job_detail", "zhipin.com/gongsi/job"]
    return any(p in href_lower for p in patterns) or len(text) >= 4


async def fetch_html(page, url: str) -> str:
    try:
        await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        except Exception:
            return ""
    except Exception:
        return ""

    # Give JS-heavy job pages a little time to render job cards.
    try:
        await page.wait_for_timeout(2500)
    except Exception:
        pass

    try:
        return await page.content()
    except Exception:
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
        full_url = urljoin(base_url, href)
        full_url = full_url.split("#")[0]
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


async def collect_matches_for_company(browser, company: Dict, rules: Dict, seen: Set[str]) -> List[JobMatch]:
    matches: List[JobMatch] = []
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="zh-CN",
        viewport={"width": 1440, "height": 1200}
    )
    page = await context.new_page()

    for source_url in company["urls"]:
        print(f"Checking {company['name']}: {source_url}")
        html = await fetch_html(page, source_url)
        if not html:
            print(f"  - Failed to load: {source_url}")
            continue

        source_info = page_title_and_text(html)
        candidates = [{"title": source_info["title"] or company["name"], "url": source_url}]
        candidates.extend(extract_links(html, source_url))

        # Deduplicate while preserving order.
        unique_candidates = []
        candidate_urls = set()
        for c in candidates:
            if c["url"] not in candidate_urls:
                unique_candidates.append(c)
                candidate_urls.add(c["url"])

        for c in unique_candidates[:MAX_LINKS_PER_SOURCE]:
            job_url = c["url"]
            job_title = c["title"]

            # Avoid crawling unrelated external domains.
            if urlparse(job_url).netloc and urlparse(source_url).netloc:
                if urlparse(job_url).netloc != urlparse(source_url).netloc:
                    continue

            detail_html = html if job_url == source_url else await fetch_html(page, job_url)
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
                print(f"  - Excluded: {title} / {exclude_hits}")
                continue

            optional_hits = contains_any(searchable_text, rules["optional_keywords"])
            job_id = make_job_id(company["name"], title, job_url)
            if job_id in seen:
                continue

            matches.append(
                JobMatch(
                    company=company["name"],
                    title=title[:160],
                    url=job_url,
                    source_url=source_url,
                    matched_korea_keywords=korea_hits,
                    matched_optional_keywords=optional_hits,
                )
            )
            seen.add(job_id)

    await context.close()
    return matches


def build_email_body(matches: List[JobMatch]) -> str:
    lines = []
    lines.append("한국 관련 키워드가 포함된 신규 채용공고가 감지되었습니다.")
    lines.append("")

    for idx, m in enumerate(matches, 1):
        lines.append(f"[{idx}] {m.company}")
        lines.append(f"공고명: {m.title}")
        lines.append(f"링크: {m.url}")
        lines.append(f"감지된 한국 키워드: {', '.join(m.matched_korea_keywords)}")
        if m.matched_optional_keywords:
            lines.append(f"보조 키워드: {', '.join(m.matched_optional_keywords)}")
        lines.append(f"수집 출처: {m.source_url}")
        lines.append("")

    lines.append("--")
    lines.append("Job Alert Bot")
    return "\n".join(lines)


def send_email(to_email: str, matches: List[JobMatch]) -> None:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM", smtp_user or "")

    if not all([smtp_host, smtp_user, smtp_password, smtp_from]):
        raise RuntimeError(
            "Missing email settings. Please set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM in GitHub Secrets."
        )

    subject = f"[채용공고 알림] 한국 관련 신규 공고 {len(matches)}건"
    body = build_email_body(matches)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Job Alert Bot", smtp_from))
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_from, [to_email], msg.as_string())


async def main() -> None:
    config = load_config()
    seen = load_seen()
    all_matches: List[JobMatch] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for company in config["companies"]:
            matches = await collect_matches_for_company(browser, company, config["rules"], seen)
            all_matches.extend(matches)
        await browser.close()

    save_seen(seen)

    if all_matches:
        print(f"Found {len(all_matches)} new matching jobs. Sending email...")
        send_email(config["email"]["to"], all_matches)
    else:
        print("No new matching jobs found.")


if __name__ == "__main__":
    asyncio.run(main())
