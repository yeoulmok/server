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

from app.estimate import AI_CAUTION_M

KST = ZoneInfo("Asia/Seoul")

STALE_FLAG = timedelta(minutes=15)
UNKNOWN_AFTER = timedelta(minutes=30)

ALERT_ORDER = ("caution", "danger", "evacuate")
SOURCE_NAMES = {"hrfco": "한강홍수통제소", "kma": "기상청", "replay": "한강홍수통제소 2026년 기록 재생(시연)"}
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


SIDO = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천", "광주광역시": "광주",
    "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종", "경기도": "경기",
    "강원특별자치도": "강원", "강원도": "강원", "충청북도": "충북", "충청남도": "충남",
    "전북특별자치도": "전북", "전라북도": "전북", "전라남도": "전남", "경상북도": "경북", "경상남도": "경남",
    "제주특별자치도": "제주",
}


def split_region(region: str | None) -> tuple[str | None, str | None]:
    """region → (도 짧은 이름, 시·군·구). 강원 계곡은 region에 도가 없음('원주시 판부면') → 강원.
    '서울특별시 관악구 신림동' → ('서울', '관악구'), '세종특별자치시 금남면' → ('세종', '세종시')."""
    parts = (region or "").split()
    if not parts:
        return None, None
    if parts[0] in SIDO:
        prov, rest = SIDO[parts[0]], parts[1:]
    else:
        prov, rest = "강원", parts
    city = next((w for w in rest if w.endswith(("시", "군", "구"))), None)
    if prov == "세종" and city is None:
        city = "세종시"
    return prov, city


def _match(v: tuple, q: str | None, province: str | None, city: str | None) -> bool:
    _, name, region, _, _ = v
    p, c = split_region(region)
    if province and p != province:
        return False
    if city and c != city:
        return False
    if q:
        ql = q.lower()
        return ql in (name or "").lower() or ql in (region or "").lower()
    return True


def filter_page(valleys: list[tuple], q=None, province=None, city=None, limit=None, offset=0) -> tuple[list[tuple], int]:
    """필터 → (이번 쪽 계곡들, 필터에 맞는 전체 수). limit 없으면 전부 id순(예전 그대로),
    있으면 이름 가나다순 → id순으로 잘라 줌 (쪽 사이에 겹침·빠짐 없게)."""
    hit = [v for v in valleys if _match(v, q, province, city)]
    if limit is None:
        return sorted(hit, key=lambda v: v[0]), len(hit)
    hit.sort(key=lambda v: (v[1] or "", v[0]))
    return hit[offset: offset + limit], len(hit)


def region_counts(valleys: list[tuple]) -> list[dict]:
    """GET /regions — 도별(많은 순) → 시·군별(많은 순, 같으면 이름순) 개수."""
    tree: dict[str, dict[str, int]] = {}
    for v in valleys:
        p, c = split_region(v[2])
        if p is None:
            continue
        tree.setdefault(p, {})
        tree[p][c or "기타"] = tree[p].get(c or "기타", 0) + 1
    out = []
    for p, cities in tree.items():
        cs = sorted(cities.items(), key=lambda x: (-x[1], x[0]))
        out.append({"province": p, "count": sum(cities.values()),
                    "cities": [{"city": c, "count": n} for c, n in cs]})
    return sorted(out, key=lambda x: (-x["count"], x["province"]))


def summary_row(valley: tuple, status: tuple | None, now: datetime, subscribed: bool,
                estimated: bool = False) -> dict:
    """GET /valleys 의 계곡 한 줄. valley = (id, name, region, lat, lon),
    status = (level, rain_1h_mm, water_rise_m, observed_at) 또는 None.
    estimated = 단계가 AI 추정(강수 기반)으로 정해졌는지 (명세 v0.3 외 추가 필드)."""
    vid, name, region, lat, lon = valley
    level, rain, rise, observed_at = status if status else (None, None, None, None)
    level, stale = freshness(level, observed_at, now)
    estimated = bool(estimated) and level != "unknown"
    return {
        "id": vid,
        "name": name,
        "region": region,
        "province": split_region(region)[0],
        "city": split_region(region)[1],
        "lat": num(lat),
        "lon": num(lon),
        "level": level,
        "rain_1h_mm": num(rain),
        "water_rise_30m_m": num(rise),
        "observed_at": fmt_time(observed_at),
        "stale": stale,
        "subscribed": subscribed,
        "estimated": estimated,
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
        "thresholds": level_limits(limits),     # v0.6: 단계별 기준값 (눈금용)
        "note": signal.reason,          # 값이 null일 때 이유 (결측·지연). 명세 외 추가 필드
    }


def level_limits(limits: dict) -> dict:
    return {lvl: num(limits.get(lvl)) for lvl in ALERT_ORDER}


def est_observation(est_rise, observed_at: datetime | None, threshold=AI_CAUTION_M) -> dict:
    """AI 추정 한 줄 (하류 수위가 없거나 비었을 때). 명세 v0.3 외 추가."""
    return {
        "type": "est_rise_3h",
        "station": "AI 추정(강수 기반)",
        "observed_at": fmt_time(observed_at),
        "value": num(est_rise),
        "unit": "m",
        "threshold": num(threshold),
        "thresholds": {"caution": num(threshold), "danger": None, "evacuate": None},
        "note": None,
        "estimated": True,
    }


def detail(valley: tuple, status: tuple | None, observations: list[dict], now: datetime,
           subscribed: bool, sources, rain_24h=None) -> dict:
    """GET /valleys/{id}. status = (level, rain, rise, observed_at, ai_explanation, ai_fallback
    [, estimated]) 또는 None."""
    vid, name = valley[0], valley[1]
    level, observed_at = (status[0], status[3]) if status else (None, None)
    ai_explanation = status[4] if status else None
    estimated = bool(status[6]) if status and len(status) > 6 else False
    level, stale = freshness(level, observed_at, now)
    estimated = estimated and level != "unknown"
    summary, actions = TEXTS[level]
    return {
        "updated_at": fmt_time(now),
        "id": vid,
        "name": name,
        "level": level,
        "summary": summary,
        "ai_explanation": ai_explanation,
        "ai_fallback": ai_explanation is None or bool(status and status[5]),
        "observations": observations,
        "rain_24h_mm": num(rain_24h),       # v0.6
        "actions": actions,
        "disclaimer": DISCLAIMER,
        "source": source_text(sources),
        "subscribed": subscribed,
        "stale": stale,
        "estimated": estimated,
    }


# ---------------------------------------------------------------------------
# 시간별 기록 (GET /valleys/{id}/history, v0.6) — 그래프용
# ---------------------------------------------------------------------------

HISTORY_STEP = timedelta(minutes=10)


def history_times(now: datetime, hours: int) -> list[datetime]:
    end = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
    n = hours * 6
    return [end - HISTORY_STEP * (n - i) for i in range(n + 1)]       # 오래된 것부터, 끝 = 지금


def _upto(series: dict, t: datetime) -> dict:
    return {k: v for k, v in series.items() if k <= t}


def history_points(times: list[datetime], rain_sources: list, water_series: list[dict],
                   status_rows: list[tuple]) -> list[dict]:
    """rain_sources = [(계산 함수, series)] (판정과 같은 함수), water_series = [series],
    status_rows = [(changed_at, level)] 오래된 것부터. 그 시각까지의 자료만 써서 계산."""
    from app.judge import merge, water_rise_30m

    points = []
    for t in times:
        rain = merge([fn(_upto(sr, t), t) for fn, sr in rain_sources]).value if rain_sources else None
        rise = merge([water_rise_30m(_upto(ws, t), t) for ws in water_series]).value if water_series else None
        level_now = next((v for ws in water_series for k, v in ws.items() if k == t and v is not None), None)
        level = None
        for changed_at, lvl in status_rows:
            if changed_at <= t:
                level = lvl
            else:
                break
        points.append({"at": fmt_time(t), "rain_1h_mm": num(rain), "water_level_m": num(level_now),
                       "water_rise_30m_m": num(rise), "level": level})
    return points
