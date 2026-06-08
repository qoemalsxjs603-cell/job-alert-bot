import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Set
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

CONFIG_PATH = Path("config.json")
SEEN_PATH = Path("seen_jobs.json")


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> Dict[str, Any]:
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
    with SEEN_PATH.open("w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def contains_any(text: str, keywords: List[str]) -> bool:
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in keywords)


def fetch_page(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ko;q=0.7",
    }
    log(f"접속: {url}")
    r = requests.get(url, headers=headers, timeout=20)
    log(f"응답 코드: {r.status_code}")
    r.raise_for_status()
    return r.text


def extract_items(company: str, url: str, html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    items = []

    page_text = normalize(soup.get_text(" ", strip=True))
    items.append({
        "id": f"{company}|{url}|PAGE",
        "company": company,
        "title": f"{company} 채용 페이지",
        "url": url,
        "text": page_text[:8000],
    })

    for a in soup.find_all("a"):
        title = normalize(a.get_text(" ", strip=True))
        href = a.get("href") or ""
        if not title:
            continue
        full_url = urljoin(url, href)
        items.append({
            "id": f"{company}|{full_url}|{title[:120]}",
            "company": company,
            "title": title[:200],
            "url": full_url,
            "text": title,
        })

    return items


def send_email(subject: str, body: str, to_email: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER") or os.getenv("EMAIL_USER")
    smtp_password = os.getenv("SMTP_PASSWORD") or os.getenv("EMAIL_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM") or smtp_user

    if not smtp_user or not smtp_password:
        log("이메일 정보 없음: 테스트 모드로 출력만 합니다.")
        log(f"제목: {subject}")
        log(body[:2000])
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_from, [to_email], msg.as_string())

    log(f"이메일 발송 완료: {to_email}")


def main() -> int:
    log("=== Job Alert Bot 시작 ===")
    config = load_config()
    seen = load_seen()

    must_keywords = config["rules"]["must_include_korea_keywords"]
    exclude_keywords = config["rules"]["exclude_keywords"]
    to_email = config["email"]["to"]

    matches = []

    for company in config["companies"]:
        company_name = company["name"]
        log(f"[{company_name}] 확인 시작")

        for url in company["urls"]:
            try:
                html = fetch_page(url)
                items = extract_items(company_name, url, html)
                log(f"후보 {len(items)}개 추출")

                for item in items:
                    text = f"{item['title']} {item['text']}"
                    if contains_any(text, must_keywords) and not contains_any(text, exclude_keywords):
                        if item["id"] not in seen:
                            matches.append(item)
                            seen.add(item["id"])
                            log(f"신규 매칭: {item['title']}")
            except Exception as e:
                log(f"오류, 다음 URL로 이동: {type(e).__name__}: {e}")

    save_seen(seen)

    if not matches:
        log("신규 매칭 공고 없음")
        log("=== Job Alert Bot 종료 ===")
        return 0

    body_lines = ["한국 관련 키워드가 포함된 신규 채용 정보가 발견되었습니다.", ""]
    for item in matches:
        body_lines += [
            f"회사: {item['company']}",
            f"제목: {item['title']}",
            f"링크: {item['url']}",
            "-" * 50,
        ]

    send_email(
        f"[채용공고 알림] 신규 한국 관련 공고 {len(matches)}건",
        "\n".join(body_lines),
        to_email,
    )

    log("=== Job Alert Bot 종료 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
