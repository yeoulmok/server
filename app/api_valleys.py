"""API 명세 v0.2 — 4번 GET /api/valleys, 5번 GET /api/valleys/{id}.

판정 결과(valley_status)와 관측값(observations)을 읽어서 앱에 준다.
app/main.py 에서 app.include_router(router) 로 붙인다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import logging

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import ask
from app import estimate as est_mod

from app import valley_view as vv
from app.judge import (KIND_RAIN_1H, KIND_RAIN_10M, LOOKBACK, ROLE_RAIN, ROLE_WATER, _series,
                       connect, rain_1h, rain_1h_direct, water_rise_30m)

router = APIRouter(prefix="/api")
log = logging.getLogger("api")

LATEST_STATUS = (
    "SELECT DISTINCT ON (valley_id) valley_id, level, rain_1h_mm, water_rise_m, observed_at, "
    "ai_explanation, ai_fallback, estimated, est_rise_m, rain_24h_mm FROM valley_status "
    "WHERE valley_id = ANY(%s) ORDER BY valley_id, changed_at DESC, id DESC"
)
RAIN_CALC = {KIND_RAIN_10M: rain_1h, KIND_RAIN_1H: rain_1h_direct}
PER_HOUR = {KIND_RAIN_10M: est_mod.hourly_from_10min, KIND_RAIN_1H: est_mod.hourly_from_1h}


def _subscribed(cur, device_id: str | None) -> set[int]:
    if not device_id:
        return set()
    cur.execute("SELECT valley_id FROM subscriptions WHERE device_id = %s", (device_id,))
    return {r[0] for r in cur.fetchall()}


def _sources(cur, valley_ids: list[int]) -> list[str]:
    cur.execute(
        "SELECT DISTINCT s.source FROM valley_stations vs JOIN stations s ON s.id = vs.station_id "
        "WHERE vs.valley_id = ANY(%s)", (valley_ids,))
    return [r[0] for r in cur.fetchall()]


def _not_found(valley_id: int) -> JSONResponse:
    return JSONResponse(status_code=404, content={
        "error": {"code": "VALLEY_NOT_FOUND", "message": "계곡 정보를 찾을 수 없어요"}})


def _valley_list(sql: str, params: tuple, x_device_id: str | None) -> dict:
    """4번·6번 공용. sql은 valleys에서 (id, name, region, lat, lon)을 고르는 쿼리."""
    now = datetime.now(vv.KST)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        valleys = cur.fetchall()
        ids = [v[0] for v in valleys]
        cur.execute(LATEST_STATUS, (ids,))
        rows = {r[0]: r for r in cur.fetchall()}
        status = {vid: r[1:5] for vid, r in rows.items()}
        sources = _sources(cur, ids)
        subs = _subscribed(cur, x_device_id)
    return {
        "updated_at": vv.fmt_time(now),
        "source": vv.source_text(sources),
        "valleys": [vv.summary_row(v, status.get(v[0]), now, v[0] in subs,
                                   bool(rows[v[0]][7]) if v[0] in rows else False) for v in valleys],
    }


@router.get("/valleys")
def list_valleys(q: str | None = None, x_device_id: str | None = Header(default=None)):
    sql = "SELECT id, name, region, lat, lon FROM valleys WHERE is_active"
    params: tuple = ()
    if q:
        sql += " AND (name ILIKE %s OR region ILIKE %s)"
        params = (f"%{q}%", f"%{q}%")
    return _valley_list(sql + " ORDER BY id", params, x_device_id)


@router.get("/valleys/{valley_id}")
def valley_detail(valley_id: int, x_device_id: str | None = Header(default=None)):
    now = datetime.now(vv.KST)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, region, lat, lon FROM valleys WHERE id = %s AND is_active",
                    (valley_id,))
        valley = cur.fetchone()
        if not valley:
            return _not_found(valley_id)
        cur.execute(LATEST_STATUS, ([valley_id],))
        row = cur.fetchone()
        status = row[1:] if row else None
        cur.execute("SELECT level, rain_1h_mm, water_rise_m FROM valley_thresholds WHERE valley_id = %s",
                    (valley_id,))
        thresholds = cur.fetchall()
        cur.execute(
            "SELECT vs.role, s.id, s.kind, s.name FROM valley_stations vs "
            "JOIN stations s ON s.id = vs.station_id WHERE vs.valley_id = %s "
            "ORDER BY vs.role DESC, s.id", (valley_id,))   # upstream_rain 먼저
        since = now - LOOKBACK
        observations = []
        rain_24h = []
        for role, sid, kind, sname in cur.fetchall():
            if role == ROLE_RAIN and kind in RAIN_CALC:
                day = _series(cur, sid, now - timedelta(hours=25))
                sig = RAIN_CALC[kind]({t: v for t, v in day.items() if t >= since}, now)
                observations.append(vv.observation("rain_1h", sname, sig, thresholds))
                rain_24h.append(est_mod.rain_sum(day, now, PER_HOUR[kind]))
            elif role == ROLE_WATER:
                sig = water_rise_30m(_series(cur, sid, since), now)
                observations.append(vv.observation("water_rise_30m", sname, sig, thresholds))
        # AI 추정은 판정 엔진이 저장한 값을 보여 준다 (수위 없음·수위 결측일 때만 값이 있음)
        if status and status[7] is not None:
            observations.append(vv.est_observation(status[7], status[3]))
        sources = _sources(cur, [valley_id])
        subs = _subscribed(cur, x_device_id)
    r24 = [r for r in rain_24h if r is not None]
    return vv.detail(valley, status, observations, now, valley_id in subs, sources,
                     max(r24) if r24 else None)


@router.get("/valleys/{valley_id}/history")
def valley_history(valley_id: int, hours: int = Query(default=3)):
    """12번 (명세 v0.6). 최근 hours시간(1~24, 기본 3) 10분 간격 기록 — 그래프용. 판정과 같은 계산."""
    hours = min(max(hours, 1), 24)
    now = datetime.now(vv.KST)
    times = vv.history_times(now, hours)
    since = times[0] - timedelta(hours=1, minutes=10)       # 첫 칸의 1시간 강수·30분 상승까지
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM valleys WHERE id = %s AND is_active", (valley_id,))
        if not cur.fetchone():
            return _not_found(valley_id)
        cur.execute("SELECT level, rain_1h_mm, water_rise_m FROM valley_thresholds WHERE valley_id = %s",
                    (valley_id,))
        thresholds = cur.fetchall()
        cur.execute("SELECT vs.role, s.id, s.kind FROM valley_stations vs "
                    "JOIN stations s ON s.id = vs.station_id WHERE vs.valley_id = %s", (valley_id,))
        links = cur.fetchall()
        rain_sources = [(RAIN_CALC[k], _series(cur, sid, since)) for role, sid, k in links
                        if role == ROLE_RAIN and k in RAIN_CALC]
        water_series = [_series(cur, sid, since) for role, sid, k in links if role == ROLE_WATER]
        # 기록 첫 칸 직전의 단계부터 (그 전에 바뀐 마지막 단계 + 기간 안의 변화)
        cur.execute("(SELECT changed_at, level FROM valley_status WHERE valley_id = %s AND changed_at <= %s "
                    " ORDER BY changed_at DESC, id DESC LIMIT 1) UNION ALL "
                    "(SELECT changed_at, level FROM valley_status WHERE valley_id = %s AND changed_at > %s) "
                    "ORDER BY 1", (valley_id, times[0], valley_id, times[0]))
        status_rows = cur.fetchall()
        sources = _sources(cur, [valley_id])
    limits = {"rain_1h_mm": vv.level_limits({t[0]: t[1] for t in thresholds}),
              "water_rise_30m_m": vv.level_limits({t[0]: t[2] for t in thresholds})}
    return {
        "updated_at": vv.fmt_time(now),
        "valley_id": valley_id,
        "hours": hours,
        "step_minutes": 10,
        "has_water": bool(water_series),
        "thresholds": limits,
        "points": vv.history_points(times, rain_sources, water_series, status_rows),
        "source": vv.source_text(sources),
    }


class AskBody(BaseModel):
    question: str | None = None


def _ask_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@router.post("/valleys/{valley_id}/ask")
def ask_valley(valley_id: int, body: AskBody, request: Request,
               x_device_id: str | None = Header(default=None)):
    """11번 (명세 v0.5). 질문 → 그 계곡의 지금 판정·관측값만으로 AI가 답한다. 실패하면 정해진 문구."""
    question = ask.normalize_question(body.question)
    if question is None:
        return _ask_error(400, "QUESTION_INVALID", "질문을 1~200자로 입력해 주세요")
    requester = x_device_id or request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown")
    if not ask.LIMITER.allow(requester):
        return _ask_error(429, "TOO_MANY_QUESTIONS", "질문이 많아요. 잠시 후 다시 물어봐 주세요")

    now = datetime.now(vv.KST)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name FROM valleys WHERE id = %s AND is_active", (valley_id,))
        valley = cur.fetchone()
        if not valley:
            return _not_found(valley_id)
        cur.execute(LATEST_STATUS, ([valley_id],))
        row = cur.fetchone()
        cur.execute("SELECT level, rain_1h_mm, water_rise_m FROM valley_thresholds WHERE valley_id = %s",
                    (valley_id,))
        thresholds = cur.fetchall()
        sources = _sources(cur, [valley_id])

    # row = (valley_id, level, rain, rise, observed_at, ai_exp, ai_fb, estimated, est_rise, rain_24h)
    status = (row[1], row[2], row[3], row[4], row[7], row[8], row[9]) if row else None
    key = (valley_id, status, question.lower())
    a = ask.LIMITER.cached(key)
    if a is None:
        if status is not None and not ask.LIMITER.ai_budget():
            a = ask.Answer(ask.fixed_answer(vv.freshness(status[0], status[3], now)[0]), True,
                           vv.freshness(status[0], status[3], now)[0], "하루 한도")
        else:
            a = ask.answer(valley[1], status, thresholds, question, now)
        ask.LIMITER.remember(key, a)
        if a.reason:
            log.info("질문 답변 고정 문구 (%s, 계곡 %s)", a.reason, valley_id)
    return {
        "updated_at": vv.fmt_time(now),
        "valley_id": valley_id,
        "question": question,
        "answer": a.answer,
        "ai_fallback": a.ai_fallback,
        "level": a.level,
        "observed_at": vv.fmt_time(status[3]) if status else None,
        "disclaimer": vv.DISCLAIMER,
        "source": vv.source_text(sources),
    }
