# US Market Report Telegram Bot

미국 경제/증시 RSS와 주요 시장 데이터를 모아 한국어 데일리 리포트를 만들고 텔레그램으로 전송하는 스크립트입니다.

## 1. 텔레그램 준비

1. 텔레그램에서 `@BotFather`에게 `/newbot`을 보내 Bot Token을 만듭니다.
2. 만든 봇에게 아무 메시지나 한 번 보냅니다.
3. 아래 주소를 브라우저에서 열어 `chat.id`를 확인합니다.

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

## 2. 환경 변수 설정

```bash
cp .env.example .env
```

`.env`에 값을 채웁니다.

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
REPORT_TIMEZONE=Asia/Seoul
NEWS_LOOKBACK_HOURS=36
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
```

`OPENAI_API_KEY`를 넣으면 핵심 뉴스 흐름을 OpenAI API로 한국어 요약합니다. 키가 없거나 API 호출이 실패하면 기본 키워드 기반 요약으로 자동 전환됩니다.

## 3. 실행

리포트 미리보기:

```bash
python3 market_report.py --print
```

텔레그램 전송:

```bash
python3 market_report.py --send
```

## 4. 매일 자동 실행

### GitHub Actions

맥북을 닫아도 매일 오전 6시에 받으려면 GitHub Actions 사용을 권장합니다.

1. GitHub에서 새 저장소를 만듭니다.
2. 이 프로젝트를 저장소에 push합니다.
3. 저장소의 `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`에서 아래 값을 등록합니다.

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
OPENAI_API_KEY
```

`OPENAI_API_KEY`는 선택입니다. 등록하지 않아도 기본 요약으로 리포트가 전송됩니다.

워크플로는 한국시간 매일 오전 6시에 실행되도록 설정되어 있습니다.

### 로컬 cron

macOS/Linux cron 예시입니다. 한국시간 매일 오전 6시에 전송하려면 `crontab -e`에 추가합니다.

```cron
0 6 * * * cd /Users/soom/Documents/workspace/us_market_report && /usr/bin/python3 market_report.py --send >> report.log 2>&1
```

미국장 마감 후 한국시간 아침에 보는 용도라면 오전 6~8시 실행이 무난합니다.

## 데이터 출처

- Yahoo Finance: 주요 지수/시장 가격, 시장 헤드라인 RSS
- MarketWatch: Top Stories RSS
- CNBC: Markets/Economy RSS
- Investing.com: Economy RSS

RSS 제공 상태나 사이트 정책에 따라 일부 출처가 일시적으로 실패할 수 있습니다. 실패한 출처는 건너뛰고 가능한 데이터만으로 리포트를 생성합니다.
