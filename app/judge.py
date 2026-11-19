"""급류 위험 판정 엔진 (규칙 기반).

계곡(valleys)마다 연결된 관측소(valley_stations)의 최근 관측값으로
  ① 상류 1시간 누적 강수량 (role = 'upstream_rain')
  ② 하류 직전 30분 수위 상승폭 (role = 'downstream_water')
를 계산하고, valley_thresholds 기준으로 단계를 정해 valley_status에 기록한다.

단계: safe < caution < danger < evacuate, 그리고 unknown(자료 지연·판정 불가)
- 결측·지연이면 마지막 값을 끌어다 쓰지 않는다.
  단, 최신 시각이 아직 비어 있으면(HRFCO가 새 10분 행을 빈 값으로 먼저 주고 나중에 채움)
  값이 있는 가장 최근 시각을 기준으로 삼는다. 그 시각도 30분을 넘기면 지연(판정 불가).
- 한쪽 신호만 있어도 그 신호가 경보 단계면 경보를 낸다.
  경보가 아니면서 한쪽이라도 자료가 없으면 unknown.
- AI 확인 단계 (app/estimate.py, 4차 실험 2단 알람):
  하류 수위 연결이 없는 계곡, 또는 수위 자료가 비었을 때만
    주의 = 24시간 강수 ≥ X mm 그리고 AI 추정 상승폭 ≥ 0.07m
  위험·대피는 지금 규칙 그대로. AI는 단계를 올리기만 한다 (unknown을 safe로 바꾸지 않음).
  수위 연결이 없는 계곡은 '수위 없음'을 결측으로 치지 않는다.
  모델 파일이 없으면 AI를 건너뜀: 수위 있는 계곡은 지금과 같고,
  수위 없는 계곡은 24시간 규칙만으로 주의를 낸다(놓치지 않게).
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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import estimate as est_mod
from app import explain, push
from app.estimate import Estimate

log = logging.getLogger("judge")

KST = ZoneInfo("Asia/Seoul")

INTERVAL_SECONDS = 5 * 60
STEP = timedelta(minutes=10)            # HRFCO 10분 자료
RAIN_STEPS = 6                          # 1시간 = 10분 × 6
RISE_WINDOW = timedelta(minutes=30)
STALE_AFTER = timedelta(minutes=30)     # 명세: 관측 30분 초과면 판정 불가
LOOKBACK = timedelta(hours=3)           # DB에서 읽어올 범위
RAIN_LOOKBACK = timedelta(hours=73)     # AI 특징(72시간 강수)용 강수 범위

NORMAL = "safe"                         # 명세 v0.2 상태 이름
UNKNOWN = "unknown"
LEVEL_RANK = {NORMAL: 0, "caution": 1, "danger": 2, "evacuate": 3}

ROLE_RAIN = "upstream_rain"
ROLE_WATER = "downstream_water"

KIND_RAIN_10M = "rainfall"        # 10분 강수량 (HRFCO) → 6개 합산
KIND_RAIN_1H = "rainfall_1h"      # 이미 1시간 누적 (기상청 AWS RN-60m) → 최신 값 그대로

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
    est: Estimate | None = None     # AI 확인 단계를 봤을 때만 (수위 없음·수위 결측)
    estimated: bool = False         # 최종 단계가 AI 추정 때문에 정해졌으면 True
    thresholds: tuple = ()          # 이 계곡 기준값 (AI 설명에 씀)

    @property
    def observed_at(self) -> datetime | None:
        times = [t for t in (self.rain.observed_at, self.rise.observed_at) if t]
        return max(times) if times else None


# ---------------------------------------------------------------------------
# 순수 계산 (DB 없이 테스트 가능)
# ---------------------------------------------------------------------------

def _latest_valued(series: Series) -> datetime | None:
    """값이 있는(None 아닌) 가장 최근 관측시각. 전부 비었으면 None."""
    valued = [t for t, v in series.items() if v is not None]
    return max(valued) if valued else None


def rain_1h(series: Series, now: datetime) -> Signal:
    """값이 있는 최신 시각 기준 직전 1시간(10분 자료 6개) 합계. 하나라도 비면 판정 불가."""
    if not series:
        return Signal(None, None, "강수 자료 없음")
    latest = _latest_valued(series)
    if latest is None:
        return Signal(None, max(series), "강수 결측")
    if now - latest > STALE_AFTER:
        return Signal(None, latest, "강수 자료 지연")
    values = [series.get(latest - STEP * i) for i in range(RAIN_STEPS)]
    if any(v is None for v in values):
        return Signal(None, latest, "강수 결측")
    return Signal(sum(values, Decimal(0)), latest)


def rain_1h_direct(series: Series, now: datetime) -> Signal:
    """값 자체가 1시간 누적 강수인 관측소. 값이 있는 최신 시각의 값을 그대로 쓴다."""
    if not series:
        return Signal(None, None, "강수 자료 없음")
    latest = _latest_valued(series)
    if latest is None:
        return Signal(None, max(series), "강수 결측")
    if now - latest > STALE_AFTER:
        return Signal(None, latest, "강수 자료 지연")
    return Signal(series[latest], latest)


def water_rise_30m(series: Series, now: datetime) -> Signal:
    """값이 있는 최신 수위 - 그 30분 전 수위. 30분 전 값이 없으면 판정 불가."""
    if not series:
        return Signal(None, None, "수위 자료 없음")
    latest = _latest_valued(series)
    if latest is None:
        return Signal(None, max(series), "수위 결측")
    if now - latest > STALE_AFTER:
        return Signal(None, latest, "수위 자료 지연")
    current, before = series[latest], series.get(latest - RISE_WINDOW)
    if before is None:
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


def _rank_max(levels: list[str]) -> str:
    return max(levels, key=LEVEL_RANK.__getitem__, default=NORMAL)


def decide(rain: Signal, rise: Signal, thresholds: list[Threshold],
           est: Estimate | None = None, has_water: bool = True) -> Verdict:
    """est: AI 추정 (없으면 None). has_water: 하류 수위관측소가 연결된 계곡인지."""
    levels: list[str] = []
    missing = False
    signals = [(rain, "rain_1h_mm")] + ([(rise, "water_rise_m")] if has_water else [])
    for sig, attr in signals:
        if sig.value is None:
            missing = True
        else:
            levels.append(level_for(sig.value, thresholds, attr))
    observed_top = _rank_max(levels)

    # AI 확인 단계는 수위가 없을 때만 (수위 있는 계곡에서 수위가 들어오면 지금 판정 그대로)
    use_ai = not has_water or rise.value is None
    if not use_ai:
        est = None
    ai_caution = False
    if est is not None:
        if est.rise is not None:
            ai_caution = est.ai_hit
        elif not has_water:
            ai_caution = est.rule_hit      # 모델이 없으면 규칙24h만으로 (놓치지 않게)
        if not has_water and est.rain_24h is None:
            missing = True                 # 24시간 강수를 못 만들면 수위 없는 계곡은 판정 불가
    elif not has_water:
        missing = True

    top = _rank_max(levels + (["caution"] if ai_caution else []))
    estimated = ai_caution and LEVEL_RANK[observed_top] < LEVEL_RANK["caution"]
    if top != NORMAL:
        level = top            # 한쪽만 있어도 경보는 낸다
    elif missing:
        level = UNKNOWN        # 경보는 아닌데 자료가 비었으면 '판정 불가'
    else:
        level = NORMAL
    return Verdict(level, rain, rise, est, estimated)


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
            "SELECT vs.role, vs.station_id, s.kind FROM valley_stations vs "
            "JOIN stations s ON s.id = vs.station_id WHERE vs.valley_id = %s",
            (valley_id,),
        )
        links = cur.fetchall()

        since = now - LOOKBACK
        rain_since = now - RAIN_LOOKBACK
        rain_calc = {KIND_RAIN_10M: rain_1h, KIND_RAIN_1H: rain_1h_direct}
        per_hour = {KIND_RAIN_10M: est_mod.hourly_from_10min, KIND_RAIN_1H: est_mod.hourly_from_1h}
        rain_series = [(kind, _series(cur, sid, rain_since)) for role, sid, kind in links
                       if role == ROLE_RAIN and kind in rain_calc]
        rain = merge([rain_calc[kind](s, now) for kind, s in rain_series])
        water_sids = [sid for role, sid, kind in links if role == ROLE_WATER]
        rise = merge([water_rise_30m(_series(cur, sid, since), now) for sid in water_sids])

    has_water = bool(water_sids)
    est = None
    if not has_water or rise.value is None:
        model = est_mod.load_model()
        est = est_mod.merge([est_mod.estimate(s, now, per_hour[kind], model) for kind, s in rain_series])
    return replace(decide(rain, rise, thresholds, est, has_water), thresholds=tuple(thresholds))


def save_status(conn, valley_id: int, v: Verdict) -> bool:
    """단계가 바뀌면 새 행(True), 같으면 최신 행 갱신(False)."""
    est = v.est
    params = (v.rain.value, v.rise.value, v.observed_at,
              est.rise if est else None, est.rain_24h if est else None, v.estimated)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT id, level FROM valley_status WHERE valley_id = %s "
            "ORDER BY changed_at DESC, id DESC LIMIT 1 FOR UPDATE",
            (valley_id,),
        )
        last = cur.fetchone()
        if last and last[1] == v.level:
            cur.execute(
                "UPDATE valley_status SET rain_1h_mm = %s, water_rise_m = %s, observed_at = %s, "
                "est_rise_m = %s, rain_24h_mm = %s, estimated = %s WHERE id = %s",
                (*params, last[0]),
            )
            return False
        cur.execute(
            "INSERT INTO valley_status (valley_id, level, rain_1h_mm, water_rise_m, observed_at, "
            "est_rise_m, rain_24h_mm, estimated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (valley_id, v.level, *params),
        )
        return True


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def _fmt(sig: Signal, unit: str) -> str:
    return f"{sig.value}{unit}" if sig.value is not None else f"- ({sig.reason})"


def _fmt_est(v: Verdict) -> str:
    e = v.est
    if e is None:
        return ""
    ai = f"{e.rise:.2f}m" if e.rise is not None else f"- ({e.reason})"
    return f" | AI 24h {e.rain_24h}mm 추정 {ai}{' → 주의(AI)' if v.estimated else ''}"


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
                try:
                    explain.after_judge(conn, valley_id, name, v, v.thresholds, changed, now)
                except Exception:
                    log.exception("%s(id=%s): AI 설명 실패 (판정은 저장됨)", name, valley_id)
                try:
                    push.after_judge(conn, valley_id, name, now)
                except Exception:
                    log.exception("%s(id=%s): 푸시 처리 실패 (판정은 저장됨)", name, valley_id)
                stats["ok"] += 1
                stats["changed"] += int(changed)
                log.info("%s: %s%s | 강수1h %s | 수위상승30m %s%s",
                         name, v.level, " (변경)" if changed else "",
                         _fmt(v.rain, "mm"), _fmt(v.rise, "m"), _fmt_est(v))
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
