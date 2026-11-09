"""API 명세 v0.2 — 4번 GET /api/valleys, 5번 GET /api/valleys/{id}.

판정 결과(valley_status)와 관측값(observations)을 읽어서 앱에 준다.
app/main.py 에서 app.include_router(router) 로 붙인다.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app import valley_view as vv
from app.judge import (KIND_RAIN_1H, KIND_RAIN_10M, LOOKBACK, ROLE_RAIN, ROLE_WATER, _series,
                       connect, rain_1h, rain_1h_direct, water_rise_30m)

router = APIRouter(prefix="/api")

LATEST_STATUS = (
    "SELECT DISTINCT ON (valley_id) valley_id, level, rain_1h_mm, water_rise_m, observed_at, "
    "ai_explanation, ai_fallback FROM valley_status "
    "WHERE valley_id = ANY(%s) ORDER BY valley_id, changed_at DESC, id DESC"
)
RAIN_CALC = {KIND_RAIN_10M: rain_1h, KIND_RAIN_1H: rain_1h_direct}


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
        "error": {"code": "VALLEY_NOT_FOUND", "message": f"계곡 {valley_id}을(를) 찾을 수 없습니다."}})


@router.get("/valleys")
def list_valleys(q: str | None = None, x_device_id: str | None = Header(default=None)):
    now = datetime.now(vv.KST)
    with connect() as conn, conn.cursor() as cur:
        sql = "SELECT id, name, region, lat, lon FROM valleys WHERE is_active"
        params: tuple = ()
        if q:
            sql += " AND (name ILIKE %s OR region ILIKE %s)"
            params = (f"%{q}%", f"%{q}%")
        cur.execute(sql + " ORDER BY id", params)
        valleys = cur.fetchall()
        ids = [v[0] for v in valleys]
        cur.execute(LATEST_STATUS, (ids,))
        status = {r[0]: r[1:5] for r in cur.fetchall()}
        sources = _sources(cur, ids)
        subs = _subscribed(cur, x_device_id)
    return {
        "updated_at": vv.fmt_time(now),
        "source": vv.source_text(sources),
        "valleys": [vv.summary_row(v, status.get(v[0]), now, v[0] in subs) for v in valleys],
    }


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
        for role, sid, kind, sname in cur.fetchall():
            if role == ROLE_RAIN and kind in RAIN_CALC:
                sig = RAIN_CALC[kind](_series(cur, sid, since), now)
                observations.append(vv.observation("rain_1h", sname, sig, thresholds))
            elif role == ROLE_WATER:
                sig = water_rise_30m(_series(cur, sid, since), now)
                observations.append(vv.observation("water_rise_30m", sname, sig, thresholds))
        sources = _sources(cur, [valley_id])
        subs = _subscribed(cur, x_device_id)
    return vv.detail(valley, status, observations, now, valley_id in subs, sources)
