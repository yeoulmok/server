"""급류 위험 판정 엔진 (규칙 기반).

계곡(valleys)마다 연결된 관측소(valley_stations)의 최근 관측값으로
  ① 상류 1시간 누적 강수량 (role = 'upstream_rain')
  ② 하류 직전 30분 수위 상승폭 (role = 'downstream_water')
를 계산하고, valley_thresholds 기준으로 단계를 정해 valley_status에 기록한다.

단계: normal < caution < danger < evacuate, 그리고 unknown(자료 지연·판정 불가)
- 결측·지연이면 마지막 값을 끌어다 쓰지 않는다.
- 한쪽 신호만 있어도 그 신호가 경보 단계면 경보를 낸다.
  경보가 아니면서 한쪽이라도 자료가 없으면 unknown.
- valley_status는 단계가 바뀔 때 새 행을 넣고(changed_at = 그 단계 시작 시각),
  같은 단계가 이어지면 최신 행의 수치와 observed_at만 갱신한다.

실행:  python -m app.judge --once   # 1회
       python -m app.judge          # 5분 루프
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

log = logging.getLogger("judge")

KST = ZoneInfo("Asia/Seoul")

INTERVAL_SECONDS = 5 * 60
STEP = timedelta(minutes=10)            # HRFCO 10분 자료
RAIN_STEPS = 6                          # 1시간 = 10분 × 6
RISE_WINDOW = timedelta(minutes=30)
STALE_AFTER = timedelta(minutes=40)     # 최신 관측이 이보다 오래되면 '지연'
LOOKBACK = timedelta(hours=3)           # DB에서 읽어올 범위

NORMAL = "normal"
UNKNOWN = "unknown"
LEVEL_RANK = {NORMAL: 0, "caution": 1, "danger": 2, "evacuate": 3}

ROLE_RAIN = "upstream_rain"
ROLE_WATER = "downstream_water"

Series = dict[datetime, "Decimal | None"]


@dataclass(frozen=True)
class Threshold:
    level: str
    rain_1h_mm: Decimal | None
    water_rise_m: Decimal | None


@dataclass(frozen=True)
class Signal:
    value: Decimal | None          # None이면 판정에 못 씀
    observed_at: datetime | None   # 계산 기준 관측시각
    reason: str | None = None      # value가 None인 이유


@dataclass(frozen=True)
class Verdict:
    level: str
    rain: Signal
    rise: Signal

    @property
    def observed_at(self) -> datetime | None:
        times = [t for t in (self.rain.observed_at, self.rise.observed_at) if t]
        return max(times) if times else None


# ---------------------------------------------------------------------------
# 순수 계산 (DB 없이 테스트 가능)
# ---------------------------------------------------------------------------

def rain_1h(series: Series, now: datetime) -> Signal:
    """최신 시각 기준 직전 1시간(10분 자료 6개) 합계. 하나라도 비면 판정 불가."""
    if not series:
        return Signal(None, None, "강수 자료 없음")
    latest = max(series)
    if now - latest > STALE_AFTER:
        return Signal(None, latest, "강수 자료 지연")
    values = [series.get(latest - STEP * i) for i in range(RAIN_STEPS)]
    if any(v is None for v in values):
        return Signal(None, latest, "강수 결측")
    return Signal(sum(values, Decimal(0)), latest)


def water_rise_30m(series: Series, now: datetime) -> Signal:
    """최신 수위 - 30분 전 수위. 둘 중 하나라도 없으면 판정 불가."""
    if not series:
        return Signal(None, None, "수위 자료 없음")
    latest = max(series)
    if now - latest > STALE_AFTER:
        return Signal(None, latest, "수위 자료 지연")
    current, before = series.get(latest), series.get(latest - RISE_WINDOW)
    if current is None or before is None:
        return Signal(None, latest, "수위 결측")
    return Signal(current - before, latest)


def merge(signals: list[Signal]) -> Signal:
    """같은 역할의 관측소가 여럿이면 쓸 수 있는 값 중 가장 위험한(큰) 값."""
    usable = [s for s in signals if s.value is not None]
    if usable:
        return max(usable, key=lambda s: s.value)
    if signals:
        return signals[0]
    return Signal(None, None, "연결된 관측소 없음")


def level_for(value: Decimal, thresholds: list[Threshold], attr: str) -> str:
    best = NORMAL
    for th in thresholds:
        limit = getattr(th, attr)
        rank = LEVEL_RANK.get(th.level)
        if limit is None or rank is None:
            continue
        if value >= limit and rank > LEVEL_RANK[best]:
            best = th.level
    return best


def decide(rain: Signal, rise: Signal, thresholds: list[Threshold]) -> Verdict:
    levels: list[str] = []
    missing = False
    for sig, attr in ((rain, "rain_1h_mm"), (rise, "water_rise_m")):
        if sig.value is None:
            missing = True
        else:
            levels.append(level_for(sig.value, thresholds, attr))

    top = max(levels, key=LEVEL_RANK.__getitem__, default=NORMAL)
    if top != NORMAL:
        level = top            # 한쪽만 있어도 경보는 낸다
    elif missing:
        level = UNKNOWN        # 경보는 아닌데 자료가 비었으면 '판정 불가'
    else:
        level = NORMAL
    return Verdict(level, rain, rise)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def connect():
    import psycopg

    return psycopg.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        connect_timeout=10,
        autocommit=True,
    )


def _series(cur, station_id: int, since: datetime) -> Series:
    cur.execute(
        "SELECT observed_at, value FROM observations "
        "WHERE station_id = %s AND observed_at >= %s",
        (station_id, since),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def evaluate_valley(conn, valley_id: int, now: datetime) -> Verdict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT level, rain_1h_mm, water_rise_m FROM valley_thresholds WHERE valley_id = %s",
            (valley_id,),
        )
        thresholds = [Threshold(*row) for row in cur.fetchall()]
        unknown_levels = {t.level for t in thresholds} - LEVEL_RANK.keys()
        if unknown_levels:
            log.warning("valley %s: 알 수 없는 단계 이름 %s 무시", valley_id, unknown_levels)

        cur.execute(
            "SELECT role, station_id FROM valley_stations WHERE valley_id = %s",
            (valley_id,),
        )
        links = cur.fetchall()

        since = now - LOOKBACK
        rain = merge([rain_1h(_series(cur, sid, since), now)
                      for role, sid in links if role == ROLE_RAIN])
        rise = merge([water_rise_30m(_series(cur, sid, since), now)
                      for role, sid in links if role == ROLE_WATER])
    return decide(rain, rise, thresholds)


def save_status(conn, valley_id: int, v: Verdict) -> bool:
    """단계가 바뀌면 새 행(True), 같으면 최신 행 갱신(False)."""
    params = (v.rain.value, v.rise.value, v.observed_at)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT id, level FROM valley_status WHERE valley_id = %s "
            "ORDER BY changed_at DESC, id DESC LIMIT 1 FOR UPDATE",
            (valley_id,),
        )
        last = cur.fetchone()
        if last and last[1] == v.level:
            cur.execute(
                "UPDATE valley_status SET rain_1h_mm = %s, water_rise_m = %s, observed_at = %s "
                "WHERE id = %s",
                (*params, last[0]),
            )
            return False
        cur.execute(
            "INSERT INTO valley_status (valley_id, level, rain_1h_mm, water_rise_m, observed_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (valley_id, v.level, *params),
        )
        return True


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def _fmt(sig: Signal, unit: str) -> str:
    return f"{sig.value}{unit}" if sig.value is not None else f"- ({sig.reason})"


def run_once() -> dict[str, int]:
    now = datetime.now(KST)
    stats = {"ok": 0, "failed": 0, "changed": 0}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM valleys WHERE is_active ORDER BY id")
            valleys = cur.fetchall()
        for valley_id, name in valleys:
            try:
                v = evaluate_valley(conn, valley_id, now)
                changed = save_status(conn, valley_id, v)
                stats["ok"] += 1
                stats["changed"] += int(changed)
                log.info("%s: %s%s | 강수1h %s | 수위상승30m %s",
                         name, v.level, " (변경)" if changed else "",
                         _fmt(v.rain, "mm"), _fmt(v.rise, "m"))
            except Exception:
                stats["failed"] += 1
                log.exception("%s(id=%s): 판정 실패", name, valley_id)
    return stats


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if "--once" in sys.argv:
        stats = run_once()
        sys.exit(1 if stats["failed"] else 0)
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception:
            log.exception("판정 회차 전체 실패")
        time.sleep(max(0.0, INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
