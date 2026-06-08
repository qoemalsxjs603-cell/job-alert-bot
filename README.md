# Job Alert Bot

Tencent, Netease, Garena 채용 페이지를 주기적으로 확인하고, 한국 관련 키워드가 포함된 신규 공고가 발견되면 이메일을 발송하는 GitHub Actions용 봇입니다.

## 현재 알림 조건

공고 제목 또는 본문에 아래 한국 관련 키워드 중 하나 이상이 포함되어야 합니다.

- 韩
- 韩国
- 韩语
- Korea
- Korean
- Seoul
- 韩国市场
- 韩国运营
- 韩国营销
- 韩国业务
- 韩国地区
- 韩国分公司
- 韩国团队
- Korea Region
- Korea Market
- South Korea

아래 제외 키워드가 포함되면 알림에서 제외합니다.

- 开发
- 程序
- 服务器
- 客户端
- 算法
- 测试
- 美术
- 设计
- 财务
- 法务
- HR
- 实习

## GitHub에 업로드할 파일 구조

```text
job-alert-bot/
├── main.py
├── config.json
├── requirements.txt
├── README.md
└── .github/
    └── workflows/
        └── job-alert.yml
```

## GitHub Secrets 설정

GitHub 저장소에서 아래 경로로 이동합니다.

```text
Settings → Secrets and variables → Actions → New repository secret
```

아래 값을 추가하세요.

```text
SMTP_HOST
SMTP_PORT
SMTP_USER
SMTP_PASSWORD
SMTP_FROM
```

### Gmail을 발송용으로 사용할 경우 예시

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=발송용 Gmail 주소
SMTP_PASSWORD=Gmail 앱 비밀번호
SMTP_FROM=발송용 Gmail 주소
```

### Naver 메일을 발송용으로 사용할 경우 예시

```text
SMTP_HOST=smtp.naver.com
SMTP_PORT=587
SMTP_USER=네이버 메일 주소
SMTP_PASSWORD=네이버 메일 비밀번호 또는 앱 비밀번호
SMTP_FROM=네이버 메일 주소
```

수신 이메일은 `config.json`의 `email.to`에 저장되어 있습니다.

## 실행 방법

GitHub에 파일 업로드 후 Actions 탭에서 `Job Alert Bot` 워크플로를 수동 실행하거나, 매시간 자동 실행을 기다리면 됩니다.

```text
Actions → Job Alert Bot → Run workflow
```

## 주의사항

BOSS直聘은 봇 감지나 로그인 제한이 있을 수 있습니다. 접속이 막히는 경우 해당 링크는 정상 수집되지 않을 수 있습니다. 이 경우 공식 채용 페이지나 로그인 없는 웹 채용 페이지를 우선 사용하는 것이 안정적입니다.
