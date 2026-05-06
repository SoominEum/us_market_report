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
MAX_DATA_AGE_DAYS = int(os.getenv("MAX_DATA_AGE_DAYS", "5"))


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


SEMI_SYMBOLS = {
    "NVDA": "Nvidia",
    "AMD": "AMD",
    "AVGO": "Broadcom",
}


SEMI_ETFS = {
    "SMH": "VanEck Semiconductor ETF",
    "SOXX": "iShares Semiconductor ETF",
}


MOMENTUM_WATCHLIST = {
    "NVDA": ("Nvidia", "반도체/AI 가속기"),
    "AMD": ("AMD", "반도체/AI 가속기"),
    "AVGO": ("Broadcom", "반도체/네트워크칩"),
    "MU": ("Micron", "메모리 반도체"),
    "ARM": ("Arm", "반도체 설계/IP"),
    "TSM": ("TSMC ADR", "파운드리"),
    "ASML": ("ASML ADR", "반도체 장비"),
    "QCOM": ("Qualcomm", "모바일 반도체"),
    "INTC": ("Intel", "종합 반도체"),
    "MRVL": ("Marvell", "데이터센터 반도체"),
    "AAPL": ("Apple", "대형 기술주/소비재"),
    "MSFT": ("Microsoft", "소프트웨어/클라우드"),
    "AMZN": ("Amazon", "이커머스/클라우드"),
    "META": ("Meta", "인터넷/AI"),
    "TSLA": ("Tesla", "전기차"),
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
    sector: str | None = None
    as_of: dt.date | None = None


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    link: str
    published: dt.datetime | None
    summary: str
    score: int


@dataclass(frozen=True)
class FearGreedIndex:
    score: float | None
    rating: str | None
    as_of: dt.datetime | None


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


def http_get(url: str, timeout: int = 20, headers: dict[str, str] | None = None) -> bytes:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
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


def fetch_fear_greed(now_utc: dt.datetime) -> FearGreedIndex:
    date = now_utc.date().isoformat()
    urls = [
        f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{date}",
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.cnn.com/markets/fear-and-greed",
    }
    for url in urls:
        try:
            payload = json.loads(http_get(url, headers=headers).decode("utf-8"))
            data = payload.get("fear_and_greed", payload)
            timestamp = data.get("timestamp")
            as_of = dt.datetime.fromisoformat(timestamp) if timestamp else None
            return FearGreedIndex(
                score=float(data["score"]) if data.get("score") is not None else None,
                rating=str(data["rating"]) if data.get("rating") else None,
                as_of=as_of,
            )
        except Exception as exc:
            print(f"warning: failed to fetch Fear & Greed data from {url}: {exc}", file=sys.stderr)
    return FearGreedIndex(score=None, rating=None, as_of=None)


def ask_openai_for_summary(markets: list[MarketMove], news: list[NewsItem], semis: list[MarketMove], etfs: list[MarketMove]) -> list[str]:
    market_text = "\n".join(
        format_move_summary(move)
        for move in markets
    )
    semi_text = "\n".join(
        format_move_summary(move)
        for move in [*semis, *etfs]
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

[반도체/ETF 데이터]
{semi_text}

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


def fetch_market_move(symbol: str, name: str, sector: str | None = None) -> MarketMove:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d"
    try:
        payload = json.loads(http_get(url).decode("utf-8"))
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        closes_with_dates = [
            (
                dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date(),
                float(close),
            )
            for timestamp, close in zip(timestamps, result["indicators"]["quote"][0]["close"])
            if close is not None
        ]
        price = closes_with_dates[-1][1] if closes_with_dates else None
        previous_close = closes_with_dates[-2][1] if len(closes_with_dates) > 1 else None
        as_of = closes_with_dates[-1][0] if closes_with_dates else None
        change_percent = None
        if price is not None and previous_close:
            change_percent = ((float(price) - float(previous_close)) / float(previous_close)) * 100
        return MarketMove(
            name=name,
            symbol=symbol,
            price=float(price) if price is not None else None,
            change_percent=change_percent,
            sector=sector,
            as_of=as_of,
        )
    except Exception as exc:
        print(f"warning: failed to fetch market data for {symbol}: {exc}", file=sys.stderr)
        return MarketMove(name=name, symbol=symbol, price=None, change_percent=None, sector=sector)


def fetch_chart_closes(symbol: str, range_: str = "1y") -> list[tuple[dt.date, float]]:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range={range_}&interval=1d"
    try:
        payload = json.loads(http_get(url).decode("utf-8"))
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        closes = result["indicators"]["quote"][0]["close"]
        return [
            (dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date(), float(close))
            for timestamp, close in zip(timestamps, closes)
            if close is not None
        ]
    except Exception as exc:
        print(f"warning: failed to fetch historical closes for {symbol}: {exc}", file=sys.stderr)
        return []


def fetch_markets() -> list[MarketMove]:
    return [fetch_market_move(symbol, name) for symbol, name in MARKET_SYMBOLS.items()]


def fetch_named_moves(symbols: dict[str, str]) -> list[MarketMove]:
    return [fetch_market_move(symbol, name) for symbol, name in symbols.items()]


def fetch_watchlist_moves() -> list[MarketMove]:
    return [fetch_market_move(symbol, name, sector) for symbol, (name, sector) in MOMENTUM_WATCHLIST.items()]


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


def format_as_of(as_of: dt.date | None) -> str:
    if not as_of:
        return "기준일 N/A"
    return f"기준 {as_of:%m-%d}"


def label_fear_greed(index: FearGreedIndex) -> str:
    if index.score is None:
        return "N/A (데이터 수집 실패)"
    score = index.score
    if score < 25:
        return "🥶 EXTREME FEAR → 공포가 과도해 변동성 확대 가능"
    if score < 45:
        return "😨 FEAR → 투자자들이 걱정 중, 변동성 증가 가능"
    if score < 55:
        return "⚖️ NEUTRAL → 투자심리가 중립권"
    if score < 75:
        return "🤑 GREED → 위험자산 선호가 우세"
    return "🔥 EXTREME GREED → 과열 경계 필요"


def label_vix(value: float | None) -> str:
    if value is None:
        return "N/A"
    if value < 15:
        return "🟢 낮은 변동성 → 시장이 비교적 안정적"
    if value < 25:
        return "🟡 보통 변동성 → 정상적인 시장 움직임"
    return "🔴 높은 변동성 → 시장 불안과 급변 가능성"


def label_ten_year_yield(value: float | None) -> str:
    if value is None:
        return "N/A"
    if value < 3:
        return "🟢 낮은 금리 → 성장주와 위험자산에 우호적"
    if value <= 4.5:
        return "🟡 보통 금리 → 시장 안정적, 큰 변화 가능성 적음"
    return "🔴 높은 금리 → 밸류에이션 부담과 변동성 요인"


def label_sp200(price: float | None, ma200: float | None) -> str:
    if price is None or ma200 is None:
        return "N/A"
    if price >= ma200:
        return "🟢 주가가 200일 평균보다 높음 → 상승 추세"
    return "🔴 주가가 200일 평균보다 낮음 → 하락/약세 추세"


def news_sentiment(news: list[NewsItem]) -> tuple[str, int]:
    positive_words = {
        "rally", "rebound", "gain", "gains", "higher", "beat", "strong", "growth",
        "optimism", "surge", "record", "boom", "upbeat", "cooling inflation",
    }
    negative_words = {
        "selloff", "fall", "falls", "lower", "drop", "drops", "weak", "miss",
        "inflation fears", "recession", "war", "tariff", "risk", "cuts", "slump",
        "down", "loss", "losses",
    }
    score = 0
    for item in news[:8]:
        text = f"{item.title} {item.summary}".lower()
        if any(word in text for word in positive_words):
            score += 1
        if any(word in text for word in negative_words):
            score -= 1
    if score >= 2:
        return "🟢 긍정적 (Positive)", score
    if score <= -2:
        return "🔴 부정적 (Negative)", score
    return "⚖️ 중립적 (Neutral)", score


def recommendation_label(
    fear_greed: FearGreedIndex,
    vix: MarketMove,
    ten_year_yield: MarketMove,
    sp500_price: float | None,
    sp500_ma200: float | None,
    news_score: int,
) -> str:
    score = 0
    if fear_greed.score is not None:
        if 35 <= fear_greed.score < 75:
            score += 1
        elif fear_greed.score >= 80:
            score -= 1
    if vix.price is not None:
        score += 1 if vix.price < 22 else -1
    yield_value = normalize_ten_year_yield(ten_year_yield.price)
    if yield_value is not None:
        score += 1 if yield_value <= 4.5 else -1
    if sp500_price is not None and sp500_ma200 is not None:
        score += 1 if sp500_price >= sp500_ma200 else -1
    if news_score >= 2:
        score += 1
    elif news_score <= -2:
        score -= 1

    if score >= 3:
        return "🟡 매수 (BUY)"
    if score >= 1:
        return "⚖️ 관망 (HOLD)"
    return "🔴 방어 (REDUCE)"


def normalize_ten_year_yield(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 10 if value > 10 else value


def build_signal_dashboard(markets: list[MarketMove], news: list[NewsItem], now_utc: dt.datetime) -> str:
    fear_greed = fetch_fear_greed(now_utc)
    by_symbol = {move.symbol: move for move in markets}
    vix = by_symbol.get("^VIX") or fetch_market_move("^VIX", "VIX")
    ten_year_yield = fetch_market_move("^TNX", "US 10Y Treasury Yield")
    ten_year_value = normalize_ten_year_yield(ten_year_yield.price)
    sp500_history = fetch_chart_closes("^GSPC", "1y")
    sp500_price = sp500_history[-1][1] if sp500_history else by_symbol.get("^GSPC", MarketMove("S&P 500", "^GSPC", None, None)).price
    sp500_ma200 = sum(close for _, close in sp500_history[-200:]) / 200 if len(sp500_history) >= 200 else None
    sentiment_label, sentiment_score = news_sentiment(news)
    recommendation = recommendation_label(fear_greed, vix, ten_year_yield, sp500_price, sp500_ma200, sentiment_score)

    fear_value = f"{fear_greed.score:.0f}" if fear_greed.score is not None else "N/A"
    vix_value = f"{vix.price:.2f}" if vix.price is not None else "N/A"
    yield_value = f"{ten_year_value:.2f}%" if ten_year_value is not None else "N/A"
    ma_value = f"{sp500_ma200:.2f}" if sp500_ma200 is not None else "N/A"

    return f"""
📉 Fear & Greed Index: {fear_value} ({label_fear_greed(fear_greed)})
📊 변동성 지수 (VIX): {vix_value} ({label_vix(vix.price)})
💰 미국 10년물 국채 금리: {yield_value} ({label_ten_year_yield(ten_year_value)})
📈 S&P 500 200일 이동평균선: {ma_value} ({label_sp200(sp500_price, sp500_ma200)})
━━━━━━━━━━━━━━━━━━━
📢 매매 추천: {recommendation}
📰 뉴스 분석 결과: {sentiment_label}
""".strip()


def format_move_summary(move: MarketMove) -> str:
    return f"{move.name}: {format_price(move.price)} ({direction_label(move.change_percent)}, {format_as_of(move.as_of)})"


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


def format_move_lines(moves: Iterable[MarketMove]) -> list[str]:
    return [
        f"- {move.name} ({move.symbol}): {format_price(move.price)} ({direction_label(move.change_percent)}, {format_as_of(move.as_of)})"
        for move in moves
    ]


def is_recent_move(move: MarketMove, now_local: dt.datetime) -> bool:
    if not move.as_of:
        return False
    return (now_local.date() - move.as_of).days <= MAX_DATA_AGE_DAYS


def surge_lines(moves: list[MarketMove], now_local: dt.datetime) -> list[str]:
    threshold = float(os.getenv("SURGE_THRESHOLD_PERCENT", "3.0"))
    surged = [
        move for move in moves
        if move.change_percent is not None
        and move.change_percent >= threshold
        and is_recent_move(move, now_local)
    ]
    surged.sort(key=lambda move: move.change_percent or 0, reverse=True)
    if not surged:
        return [f"- 관심 종목군에서 +{threshold:.1f}% 이상 급등한 종목은 확인되지 않았습니다."]
    return [
        f"- {move.name} ({move.symbol}): {direction_label(move.change_percent)} ({format_as_of(move.as_of)}) / 관련 섹터: {move.sector or '분류 없음'}"
        for move in surged[:5]
    ]


def korea_impact(markets: list[MarketMove], semis: list[MarketMove], etfs: list[MarketMove], watchlist: list[MarketMove]) -> list[str]:
    by_symbol = {move.symbol: move for move in [*markets, *semis, *etfs, *watchlist]}
    nasdaq = by_symbol.get("^IXIC")
    sp500 = by_symbol.get("^GSPC")
    vix = by_symbol.get("^VIX")
    dollar = by_symbol.get("DX-Y.NYB")
    wti = by_symbol.get("CL=F")
    semi_values = [
        move.change_percent for move in [*semis, *etfs]
        if move.change_percent is not None
    ]
    semi_avg = sum(semi_values) / len(semi_values) if semi_values else None

    lines = []
    if nasdaq and nasdaq.change_percent is not None:
        if nasdaq.change_percent >= 0.4:
            lines.append("- Nasdaq 강세는 다음날 한국 성장주와 KOSDAQ 투자심리에 우호적인 재료입니다.")
        elif nasdaq.change_percent <= -0.4:
            lines.append("- Nasdaq 약세는 다음날 한국 성장주와 KOSDAQ에 부담으로 작용할 수 있습니다.")
        else:
            lines.append("- Nasdaq 변동폭이 제한적이라 한국 성장주 영향은 업종별 재료에 따라 갈릴 가능성이 큽니다.")

    if semi_avg is not None:
        if semi_avg >= 0.5:
            lines.append(f"- 미국 반도체 주요주/ETF 평균이 {semi_avg:+.2f}%로 강해 삼성전자, SK하이닉스 등 반도체 대형주 심리에 긍정적입니다.")
        elif semi_avg <= -0.5:
            lines.append(f"- 미국 반도체 주요주/ETF 평균이 {semi_avg:+.2f}%로 약해 국내 반도체 밸류체인에는 부담 요인입니다.")
        else:
            lines.append(f"- 미국 반도체 주요주/ETF 평균은 {semi_avg:+.2f}%로 중립권이라 국내 반도체주는 개별 뉴스 영향이 더 커질 수 있습니다.")

    if dollar and dollar.change_percent is not None and dollar.change_percent >= 0.3:
        lines.append("- 달러 강세는 원화 약세 압력으로 이어질 수 있어 외국인 수급에는 다소 부담입니다.")
    elif dollar and dollar.change_percent is not None and dollar.change_percent <= -0.3:
        lines.append("- 달러 약세는 원화와 신흥국 수급에 우호적으로 해석될 수 있습니다.")

    if vix and vix.change_percent is not None and vix.change_percent <= -3:
        lines.append("- VIX 하락은 위험선호 회복 신호라 한국 증시 전반의 투자심리에 보탬이 될 수 있습니다.")
    elif vix and vix.change_percent is not None and vix.change_percent >= 3:
        lines.append("- VIX 상승은 위험회피 신호라 한국장 초반 변동성을 키울 수 있습니다.")

    if wti and wti.change_percent is not None and abs(wti.change_percent) >= 1:
        direction = "상승" if wti.change_percent > 0 else "하락"
        lines.append(f"- WTI 유가 {direction}은 에너지, 화학, 항공, 운송 업종의 상대 흐름에 영향을 줄 수 있습니다.")

    if not lines and sp500 and sp500.change_percent is not None:
        lines.append(f"- S&P 500 등락률이 {sp500.change_percent:+.2f}%로 제한적이라 한국장 영향은 중립에 가깝습니다.")
    return lines[:5]


def theme_count(items: list[NewsItem], words: set[str]) -> int:
    count = 0
    for item in items[:8]:
        text = f"{item.title} {item.summary}".lower()
        if any(word in text for word in words):
            count += 1
    return count


def news_themes(items: list[NewsItem], moves: list[MarketMove], semis: list[MarketMove], etfs: list[MarketMove]) -> list[str]:
    if os.getenv("OPENAI_API_KEY"):
        try:
            return ask_openai_for_summary(moves, items, semis, etfs)
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
    semis = fetch_named_moves(SEMI_SYMBOLS)
    semi_etfs = fetch_named_moves(SEMI_ETFS)
    watchlist = fetch_watchlist_moves()
    news = fetch_news(now_utc)
    dashboard = build_signal_dashboard(markets, news, now_utc)

    market_lines = format_move_lines(markets)
    semi_lines = format_move_lines(semis)
    semi_etf_lines = format_move_lines(semi_etfs)

    report = f"""
{dashboard}

🇺🇸 미국 증시 데일리 리포트
기준 시각: {now_local:%Y-%m-%d %H:%M} {timezone_name}

[시장 상황]
{chr(10).join(market_lines)}

[요약]
{market_tone(markets)}

[반도체 주요 종목]
{chr(10).join(semi_lines)}

[반도체 ETF]
{chr(10).join(semi_etf_lines)}

[급등 종목 체크]
{chr(10).join(surge_lines(watchlist, now_local))}

[핵심 뉴스 흐름]
{chr(10).join(news_themes(news, markets, semis, semi_etfs))}

[다음날 한국 증시 영향]
{chr(10).join(korea_impact(markets, semis, semi_etfs, watchlist))}

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
