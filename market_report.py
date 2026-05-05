#!/usr/bin/env python3
"""
Daily US market report sender.

Fetches market moves and recent economy/market news, builds a concise Korean
report, and optionally sends it to Telegram.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Seoul"
LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "36"))
USER_AGENT = "us-market-report/1.0 (+https://example.local)"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")


MARKET_SYMBOLS = {
    "^GSPC": "S&P 500",
    "^DJI": "Dow",
    "^IXIC": "Nasdaq",
    "^RUT": "Russell 2000",
    "^VIX": "VIX",
    "DX-Y.NYB": "US Dollar Index",
    "GC=F": "Gold",
    "CL=F": "WTI Oil",
}


NEWS_FEEDS = [
    {
        "name": "Yahoo Finance",
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EDJI,%5EIXIC&region=US&lang=en-US",
    },
    {
        "name": "MarketWatch",
        "url": "https://www.marketwatch.com/rss/topstories",
    },
    {
        "name": "CNBC Markets",
        "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    },
    {
        "name": "CNBC Economy",
        "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    },
    {
        "name": "Investing.com Economy",
        "url": "https://www.investing.com/rss/news_95.rss",
    },
]


KEYWORDS = {
    "fed": 8,
    "fomc": 8,
    "powell": 7,
    "inflation": 7,
    "cpi": 7,
    "pce": 7,
    "jobs": 6,
    "payroll": 6,
    "unemployment": 6,
    "treasury": 6,
    "yield": 6,
    "rates": 5,
    "gdp": 5,
    "recession": 5,
    "tariff": 5,
    "trade": 4,
    "dollar": 4,
    "oil": 4,
    "earnings": 4,
    "nvidia": 4,
    "apple": 3,
    "microsoft": 3,
    "amazon": 3,
    "tesla": 3,
    "ai": 3,
    "stocks": 3,
    "nasdaq": 3,
    "s&p": 3,
    "wall street": 3,
}


@dataclass(frozen=True)
class MarketMove:
    name: str
    symbol: str
    price: float | None
    change_percent: float | None


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    link: str
    published: dt.datetime | None
    summary: str
    score: int


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def http_get(url: str, timeout: int = 20) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def score_news(title: str, summary: str) -> int:
    haystack = f"{title} {summary}".lower()
    score = 0
    for keyword, weight in KEYWORDS.items():
        if keyword in haystack:
            score += weight
    return score


def fetch_news(now_utc: dt.datetime) -> list[NewsItem]:
    cutoff = now_utc - dt.timedelta(hours=LOOKBACK_HOURS)
    items: list[NewsItem] = []
    seen_titles: set[str] = set()

    for feed in NEWS_FEEDS:
        try:
            data = http_get(feed["url"])
            root = ET.fromstring(data)
        except Exception as exc:
            print(f"warning: failed to fetch {feed['name']}: {exc}", file=sys.stderr)
            continue

        for node in root.findall(".//item"):
            title = strip_html(node.findtext("title") or "")
            if not title:
                continue
            normalized_title = re.sub(r"\W+", " ", title.lower()).strip()
            if normalized_title in seen_titles:
                continue
            link = (node.findtext("link") or "").strip()
            published = parse_datetime(node.findtext("pubDate") or node.findtext("published"))
            if published and published < cutoff:
                continue
            summary = strip_html(node.findtext("description") or node.findtext("summary") or "")
            score = score_news(title, summary)
            if score <= 0 and len(items) > 20:
                continue
            seen_titles.add(normalized_title)
            items.append(
                NewsItem(
                    title=title,
                    source=feed["name"],
                    link=link,
                    published=published,
                    summary=summary,
                    score=score,
                )
            )

    items.sort(
        key=lambda item: (
            item.score,
            item.published.timestamp() if item.published else 0,
        ),
        reverse=True,
    )
    return items[:12]


def openai_headers() -> dict[str, str]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def ask_openai_for_summary(markets: list[MarketMove], news: list[NewsItem]) -> list[str]:
    market_text = "\n".join(
        f"{move.name}: {format_price(move.price)} ({direction_label(move.change_percent)})"
        for move in markets
    )
    news_text = "\n".join(
        f"{index}. {item.title} ({item.source}) - {item.summary[:220]}"
        for index, item in enumerate(news[:8], start=1)
    )
    prompt = f"""
아래 미국 시장 데이터와 경제/증시 헤드라인을 바탕으로 한국어 데일리 리포트의
'핵심 뉴스 흐름' 섹션을 작성해줘.

조건:
- 정확히 4개의 bullet만 작성
- 각 bullet은 '- '로 시작
- 과장하지 말고, 데이터에서 근거를 찾을 수 있는 내용만 작성
- 투자 조언처럼 쓰지 말고 시장 해석으로 작성
- 출처 링크는 쓰지 말 것

[시장 데이터]
{market_text}

[뉴스]
{news_text}
""".strip()
    payload = {
        "model": os.getenv("OPENAI_MODEL", OPENAI_MODEL),
        "input": prompt,
        "temperature": 0.2,
        "max_output_tokens": 700,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers=openai_headers(),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))

    text = data.get("output_text")
    if not text:
        parts = []
        for output in data.get("output", []):
            for content in output.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    parts.append(content.get("text", ""))
        text = "\n".join(parts)

    bullets = [line.strip() for line in (text or "").splitlines() if line.strip().startswith("-")]
    if not bullets:
        raise RuntimeError("OpenAI response did not contain bullet lines.")
    return bullets[:4]


def fetch_market_move(symbol: str, name: str) -> MarketMove:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d"
    try:
        payload = json.loads(http_get(url).decode("utf-8"))
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        closes = [value for value in result["indicators"]["quote"][0]["close"] if value is not None]
        price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
        previous_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        if previous_close is None and len(closes) > 1:
            previous_close = closes[-2]
        if price is None:
            price = closes[-1] if closes else None
        change_percent = None
        if price is not None and previous_close:
            change_percent = ((float(price) - float(previous_close)) / float(previous_close)) * 100
        return MarketMove(name=name, symbol=symbol, price=float(price) if price is not None else None, change_percent=change_percent)
    except Exception as exc:
        print(f"warning: failed to fetch market data for {symbol}: {exc}", file=sys.stderr)
        return MarketMove(name=name, symbol=symbol, price=None, change_percent=None)


def fetch_markets() -> list[MarketMove]:
    return [fetch_market_move(symbol, name) for symbol, name in MARKET_SYMBOLS.items()]


def direction_label(change_percent: float | None) -> str:
    if change_percent is None:
        return "N/A"
    if change_percent > 0:
        return f"+{change_percent:.2f}%"
    return f"{change_percent:.2f}%"


def format_price(price: float | None) -> str:
    if price is None:
        return "N/A"
    if price >= 100:
        return f"{price:,.0f}"
    return f"{price:,.2f}"


def market_tone(moves: Iterable[MarketMove]) -> str:
    core = [move.change_percent for move in moves if move.symbol in {"^GSPC", "^DJI", "^IXIC"} and move.change_percent is not None]
    if not core:
        return "주요 지수 데이터가 부족해 방향성을 판단하기 어렵습니다."
    positives = sum(1 for value in core if value > 0.15)
    negatives = sum(1 for value in core if value < -0.15)
    avg = sum(core) / len(core)
    if positives >= 2:
        return f"위험자산 선호가 우세했습니다. 주요 3대 지수 평균 등락률은 {avg:+.2f}%입니다."
    if negatives >= 2:
        return f"방어적인 흐름이 강했습니다. 주요 3대 지수 평균 등락률은 {avg:+.2f}%입니다."
    return f"지수별 방향이 엇갈린 혼조세였습니다. 주요 3대 지수 평균 등락률은 {avg:+.2f}%입니다."


def theme_count(items: list[NewsItem], words: set[str]) -> int:
    count = 0
    for item in items[:8]:
        text = f"{item.title} {item.summary}".lower()
        if any(word in text for word in words):
            count += 1
    return count


def news_themes(items: list[NewsItem], moves: list[MarketMove]) -> list[str]:
    if os.getenv("OPENAI_API_KEY"):
        try:
            return ask_openai_for_summary(moves, items)
        except Exception as exc:
            print(f"warning: OpenAI summary failed, using fallback summary: {exc}", file=sys.stderr)

    if not items:
        return ["- 경제/시장 뉴스 피드에서 최근 주요 항목을 충분히 수집하지 못했습니다."]

    themes = []
    rates = theme_count(items, {"fed", "fomc", "powell", "inflation", "cpi", "pce", "treasury", "yield", "rates"})
    growth = theme_count(items, {"jobs", "payroll", "unemployment", "gdp", "recession", "consumer", "spending"})
    commodities = theme_count(items, {"oil", "gold", "dollar", "energy", "crude", "war", "iran"})
    earnings = theme_count(items, {"earnings", "guidance", "profit", "revenue", "nvidia", "apple", "microsoft", "amazon", "tesla"})

    vix = next((move for move in moves if move.symbol == "^VIX"), None)
    dollar = next((move for move in moves if move.symbol == "DX-Y.NYB"), None)

    if rates:
        themes.append(f"- 금리/물가 이슈가 뉴스 흐름의 중심입니다. 연준, 인플레이션, 국채금리 관련 헤드라인이 {rates}건 포착됐습니다.")
    if growth:
        themes.append(f"- 경기 지표와 성장 둔화 여부도 함께 주목받고 있습니다. 고용, GDP, 소비 관련 헤드라인이 {growth}건 포함됐습니다.")
    if earnings:
        themes.append(f"- 기업 실적과 대형 기술주 뉴스가 지수 방향에 영향을 주는 재료로 잡힙니다. 관련 헤드라인은 {earnings}건입니다.")
    if commodities:
        themes.append(f"- 유가, 달러, 금 등 매크로 변수 관련 뉴스가 확인됩니다. 관련 헤드라인은 {commodities}건입니다.")
    if vix and vix.change_percent is not None:
        direction = "낮아지며" if vix.change_percent < 0 else "높아지며"
        themes.append(f"- VIX는 {direction} 변동성 기대가 전일보다 {abs(vix.change_percent):.2f}% 움직였습니다.")
    if dollar and dollar.change_percent is not None and abs(dollar.change_percent) >= 0.3:
        direction = "강세" if dollar.change_percent > 0 else "약세"
        themes.append(f"- 달러 인덱스가 {direction}를 보이며 글로벌 유동성/원자재 가격에도 영향을 줄 수 있는 환경입니다.")

    if not themes:
        top_titles = ", ".join(item.title for item in items[:2])
        themes.append(f"- 수집된 주요 헤드라인은 특정 테마에 쏠리기보다 개별 이슈 중심입니다: {top_titles}")
    return themes[:5]


def source_lines(items: list[NewsItem]) -> list[str]:
    lines = []
    for index, item in enumerate(items[:8], start=1):
        link = item.link or "링크 없음"
        lines.append(f"{index}. {item.title} - {item.source}\n   {link}")
    if not lines:
        lines.append("RSS 수집 결과 없음")
    return lines


def build_report(timezone_name: str = DEFAULT_TIMEZONE) -> str:
    timezone = ZoneInfo(timezone_name)
    now_local = dt.datetime.now(timezone)
    now_utc = now_local.astimezone(dt.timezone.utc)
    markets = fetch_markets()
    news = fetch_news(now_utc)

    market_lines = [
        f"- {move.name}: {format_price(move.price)} ({direction_label(move.change_percent)})"
        for move in markets
    ]

    report = f"""
🇺🇸 미국 증시 데일리 리포트
기준 시각: {now_local:%Y-%m-%d %H:%M} {timezone_name}

[시장 상황]
{chr(10).join(market_lines)}

[요약]
{market_tone(markets)}

[핵심 뉴스 흐름]
{chr(10).join(news_themes(news, markets))}

[주요 출처]
{chr(10).join(source_lines(news))}

자동 생성 리포트입니다. 투자 판단 전 원문과 실시간 가격을 확인하세요.
""".strip()
    return report


def chunk_message(message: str, limit: int = 3900) -> list[str]:
    chunks = []
    remaining = message
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in chunk_message(message):
        body = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create and send a daily US market report.")
    parser.add_argument("--send", action="store_true", help="send the report to Telegram")
    parser.add_argument("--print", action="store_true", help="print the report to stdout")
    parser.add_argument("--timezone", default=os.getenv("REPORT_TIMEZONE", DEFAULT_TIMEZONE))
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    report = build_report(args.timezone)

    if args.print or not args.send:
        print(report)
    if args.send:
        send_telegram(report)
        print("sent telegram report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
