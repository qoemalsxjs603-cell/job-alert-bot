import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Any, Set

import requests
from bs4 import BeautifulSoup


CONFIG_PATH = Path("config.json")
SEEN_PATH = Path("seen_jobs.json")


def log(message: str) -> None:
    print(message, flush=True)


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError("config.json 파일을 찾을 수 없습니다.")
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
    lower_text = text.lower()
    return any(k.lower() in lower_text for k in keywords)


def fetch_page(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ko;q=0.7",
    }
    log(f"  - 접속 시도: {url}")
    response = requests.get(url, headers=headers, timeout=20)
    log(f"  - 응답 코드: {response.status_code}")
    response.raise_for_status()
    return response.text


def extract_candidates(company: str, url: str, html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    text = normalize(soup.get_text(" ", strip=True))

    candidates = []

    # 1차: 페이지 전체 텍스트를 하나의 후보로 검사
    candidates.append({
        "company": company,
        "title": f"{company} 채용 페이지 내 한국 관련 키워드 발견",
        "url": url,
        "text": text[:5000],
        "id": f"{company}|{url}|page"
    })

    # 2차: 링크 단위로도 후보 생성
    for a in soup.find_all("a"):
        title = normalize(a.get_text(" ", strip=True))
        href = a.get("href") or ""
        if not title:
            continue
        if href.startswith("//"):
            full_url = "https:" + href
        elif href.startswith("http"):
            full_url = href
        elif href.startswith("/"):
            # URL 도메인 보정
            from urllib.parse import urljoin
            full_url = urljoin(url, href)
        else:
            from urllib.parse import urljoin
            full_url = urljoin(url, href)

        candidates.append({
            "company": company,
            "title": title[:200],
            "url": full_url,
            "text": title,
            "id": f"{company}|{full_url}|{title[:80]}"
        })

    return candidates


def send_email(subject: str, body: str, to_email: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER") or os.getenv("EMAIL_USER")
    smtp_password = os.getenv("SMTP_PASSWORD") or os.getenv("EMAIL_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM") or smtp_user

    if not smtp_user or not smtp_password:
        log("  - 이메일 발송 정보가 없어 테스트 모드로 처리합니다.")
        log("  - 아래 내용이 실제 이메일로 발송될 예정입니다.")
        log(subject)
        log(body[:1000])
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_from, [to_email], msg.as_string())

    log(f"  - 이메일 발송 완료: {to_email}")


def main() -> int:
    log("=== Job Alert Bot 시작 ===")
    config = load_config()
    seen = load_seen()

    must_keywords = config["rules"]["must_include_korea_keywords"]
    exclude_keywords = config["rules"]["exclude_keywords"]
    to_email = config["email"]["to"]

    log(f"알림 받을 이메일: {to_email}")
    log(f"필수 키워드: {', '.join(must_keywords)}")
    log(f"제외 키워드: {', '.join(exclude_keywords)}")

    new_matches = []

    for company in config["companies"]:
        name = company["name"]
        log(f"\n[{name}] 확인 시작")

        for url in company["urls"]:
            try:
                html = fetch_page(url)
                candidates = extract_candidates(name, url, html)
                log(f"  - 후보 {len(candidates)}개 추출")

                for item in candidates:
                    check_text = f"{item['title']} {item['text']}"
                    has_korea = contains_any(check_text, must_keywords)
                    has_exclude = contains_any(check_text, exclude_keywords)

                    if has_korea and not has_exclude and item["id"] not in seen:
                        new_matches.append(item)
                        seen.add(item["id"])
                        log(f"  - 신규 매칭 발견: {item['title']}")

            except Exception as e:
                log(f"  - 오류 발생, 다음 URL로 넘어갑니다: {type(e).__name__}: {e}")

    save_seen(seen)

    if not new_matches:
        log("\n신규 매칭 공고 없음")
        log("=== Job Alert Bot 종료 ===")
        return 0

    lines = ["한국 관련 키워드가 포함된 신규 채용 정보가 발견되었습니다.", ""]
    for item in new_matches:
        lines.extend([
            f"회사: {item['company']}",
            f"제목: {item['title']}",
            f"링크: {item['url']}",
            "-" * 40,
        ])

    subject = f"[채용공고 알림] 신규 한국 관련 공고 {len(new_matches)}건"
    body = "\n".join(lines)
    send_email(subject, body, to_email)

    log("=== Job Alert Bot 종료 ===")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        log(f"치명적 오류: {type(e).__name__}: {e}")
        raise
