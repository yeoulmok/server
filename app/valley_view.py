"""앱용 계곡 상태 응답 모양 만들기 (API 명세 v0.2, 4번·5번).

DB나 FastAPI 없이 테스트할 수 있게 계산·형식만 모아 둔다.
- 시각은 한국시간 ISO8601 분 단위 ("2026-11-06T23:00")
- 값이 없으면 null (0으로 보내지 않음)
- 관측 15분 초과 → stale: true, 30분 초과 또는 자료 없음 → level: "unknown"
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

STALE_FLAG = timedelta(minutes=15)
UNKNOWN_AFTER = timedelta(minutes=30)

ALERT_ORDER = ("caution", "danger", "evacuate")
SOURCE_NAMES = {"hrfco": "한강홍수통제소", "kma": "기상청"}
DISCLAIMER = "판단 보조 정보이며 공식 재난경보를 대체하지 않습니다."

# 고정 문구 (AI 설명이 없을 때). 문구는 팀 검토 전 초안.
TEXTS: dict[str, tuple[str, list[str]]] = {
    "safe": ("지금은 위험 신호가 없어요",
             ["상류 날씨 변화에 계속 주의하기"]),
    "caution": ("상류에 비가 많이 오고 있어요",
                ["물가에서 떨어지기", "높은 곳으로 이동 준비"]),
    "danger": ("물이 빠르게 불어날 수 있어요",
               ["즉시 물에서 나오기", "소지품을 챙겨 높은 곳으로 이동"]),
    "evacuate": ("지금 바로 대피하세요",
                 ["즉시 높은 곳으로 대피", "계곡을 건너지 않기", "고립되면 119 신고"]),
    "unknown": ("관측 자료가 늦거나 없어 판정할 수 없어요",
                ["현장 물 상황을 직접 확인하기", "비가 오면 물가에서 떨어지기"]),
}


def fmt_time(dt: datetime | None) -> str | None:
    return dt.astimezone(KST).strftime("%Y-%m-%dT%H:%M") if dt else None


def num(v) -> float | None:
    return float(v) if v is not None else None


def freshness(level: str | None, observed_at: datetime | None, now: datetime) -> tuple[str, bool]:
    """(최종 level, stale). 판정 엔진이 멈춰 있어도 오래된 상태를 '안전'으로 내보내지 않는다."""
    if level is None or observed_at is None:
        return "unknown", True
    age = now - observed_at
    if age > UNKNOWN_AFTER:
        return "unknown", True
    return level, age > STALE_FLAG


def source_text(sources) -> str:
    order = list(SOURCE_NAMES)
    uniq = sorted(set(sources), key=lambda s: order.index(s) if s in order else len(order))
    return "자료: " + ", ".join(SOURCE_NAMES.get(s, s) for s in uniq)


def pick_threshold(value, limits: dict[str, Decimal | None]) -> float | None:
    """값이 넘은 가장 높은 단계의 기준값. 아무것도 안 넘었거나 값이 없으면 '주의' 기준값."""
    best = limits.get("caution")
    if value is not None:
        for level in ALERT_ORDER:
            lim = limits.get(level)
            if lim is not None and value >= lim:
                best = lim
    return num(best)


def summary_row(valley: tuple, status: tuple | None, now: datetime, subscribed: bool) -> dict:
    """GET /valleys 의 계곡 한 줄. valley = (id, name, region, lat, lon),
    status = (level, rain_1h_mm, water_rise_m, observed_at) 또는 None."""
    vid, name, region, lat, lon = valley
    level, rain, rise, observed_at = status if status else (None, None, None, None)
    level, stale = freshness(level, observed_at, now)
    return {
        "id": vid,
        "name": name,
        "region": region,
        "lat": num(lat),
        "lon": num(lon),
        "level": level,
        "rain_1h_mm": num(rain),
        "water_rise_30m_m": num(rise),
        "observed_at": fmt_time(observed_at),
        "stale": stale,
        "subscribed": subscribed,
    }


def observation(kind: str, station: str, signal, thresholds: list[tuple]) -> dict:
    """경보 상세의 관측 근거 한 줄. kind = 'rain_1h' | 'water_rise_30m',
    thresholds = [(level, rain_1h_mm, water_rise_m), ...]."""
    col = 1 if kind == "rain_1h" else 2
    limits = {t[0]: t[col] for t in thresholds}
    return {
        "type": kind,
        "station": station,
        "observed_at": fmt_time(signal.observed_at),
        "value": num(signal.value),
        "unit": "mm" if kind == "rain_1h" else "m",
        "threshold": pick_threshold(signal.value, limits),
        "note": signal.reason,          # 값이 null일 때 이유 (결측·지연). 명세 외 추가 필드
    }


def detail(valley: tuple, status: tuple | None, observations: list[dict], now: datetime,
           subscribed: bool, sources) -> dict:
    """GET /valleys/{id}. status = (level, rain, rise, observed_at, ai_explanation, ai_fallback) 또는 None."""
    vid, name = valley[0], valley[1]
    level, observed_at = (status[0], status[3]) if status else (None, None)
    ai_explanation = status[4] if status else None
    level, stale = freshness(level, observed_at, now)
    summary, actions = TEXTS[level]
    return {
        "id": vid,
        "name": name,
        "level": level,
        "summary": summary,
        "ai_explanation": ai_explanation,
        "ai_fallback": ai_explanation is None or bool(status and status[5]),
        "observations": observations,
        "actions": actions,
        "disclaimer": DISCLAIMER,
        "source": source_text(sources),
        "subscribed": subscribed,
        "stale": stale,
    }
