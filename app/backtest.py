"""19곳 일괄 재현 (#58) — 계곡마다 지난 큰비 날 몇 번을 지금 판정 엔진에 다시 넣어 채점. DB·푸시 안 건드림.

  python -m app.backtest                       # 모든 계곡, 2024·2025년 6~9월 큰비 3번씩
  python -m app.backtest --valley 내리계곡 --top 5
  python -m app.backtest --years 2025

하는 일
1. 계곡의 비 관측소(기상청 AWS면 가장 가까운 HRFCO 비 관측소)에서 1시간 강수로 큰비 날을 고름
   (하루 최대 1시간 강수 ≥ 10mm, 서로 3일 이상 떨어진 날, 큰 순서)
2. 그날 00:00~23:50을 reconstruct와 같은 함수로 10분마다 판정 (그 시각까지 자료만)
3. A 계곡(수위 있음)은 실제 수위로 채점:
   - 실제 상승 = 그날 최고 수위 - 그날 00시 수위
   - 적중: 실제 상승 ≥ 0.10m 인데 그 전에 주의 이상
   - 헛경보: 주의 이상을 냈는데 실제 상승 < 0.10m
   - 놓침: 실제 상승 ≥ 0.10m 인데 주의를 못 냄 (또는 최고점 뒤에 냄)
   - 평소 대비 규칙(#59, 서버에 있음): 수위가 직전 24시간 최저보다 0.30m 이상 높으면 주의 → 언제 켜졌나
4. 결과: 화면 표 + data/backtest/summary.csv (보고서·교수님 상담용)

한계: 기준값은 금대리 임시값(#20). B 계곡은 정답(실제 수위)이 없어 "언제 경고했나"까지만.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app import estimate as est_mod
from app import judge as J
from app import reconstruct as R
from app import replay

KST = R.KST
STEP = R.STEP
OUT = Path(__file__).resolve().parents[1] / "data" / "backtest"
MIN_EVENT_MM = Decimal("10")
GAP_DAYS = 3
RISE_HIT_M = Decimal("0.10")            # 채점용 "실제로 물이 불었다" (주의 기준과 같음)
HOLD_M = J.HOLD_M                       # #59 평소 대비 규칙 (서버와 같은 값)
MONTHS = (6, 7, 8, 9)


# ---------------------------------------------------------------------------
# 순수 계산
# ---------------------------------------------------------------------------

def pick_events(hourly: dict[datetime, Decimal | None], top: int,
                min_mm: Decimal = MIN_EVENT_MM, gap_days: int = GAP_DAYS) -> list[tuple[date, Decimal]]:
    """1시간 강수 → (날짜, 그날 최대 1시간 강수) 큰 순서로, 서로 gap_days 이상 떨어진 날 top개."""
    daymax: dict[date, Decimal] = {}
    for t, v in hourly.items():
        if v is None:
            continue
        d = (t - timedelta(minutes=1)).date()          # 00:00 값은 전날 23~24시 비
        daymax[d] = max(daymax.get(d, Decimal(0)), v)
    out: list[tuple[date, Decimal]] = []
    for d, mm in sorted(daymax.items(), key=lambda x: (-x[1], x[0])):
        if mm < min_mm:
            break
        if all(abs((d - o).days) >= gap_days for o, _ in out):
            out.append((d, mm))
        if len(out) >= top:
            break
    return out


def hold_series(water: dict[datetime, Decimal | None]) -> dict[datetime, Decimal]:
    """t마다 수위 - 직전 24시간 최저 (실험 규칙용)."""
    pts = sorted((t, v) for t, v in water.items() if v is not None)
    out = {}
    for i, (t, v) in enumerate(pts):
        lo = min(w for s, w in pts[: i + 1] if s >= t - timedelta(hours=24))
        out[t] = v - lo
    return out


@dataclass
class Score:
    day: date
    max_1h: Decimal | None
    first_caution: datetime | None       # 여울목 (지금 서버와 같은 판정)
    top_level: str
    rule_only_first: datetime | None
    rise: Decimal | None = None          # A만: 그날 최고 - 00시
    peak_at: datetime | None = None
    with_water_first: datetime | None = None
    hold_first: datetime | None = None
    verdict: str = ""                    # A만: 적중 / 헛경보 / 놓침 / 조용(비 왔지만 물 안 붊, 경고도 없음)


def score_day(day: date, rows: list[R.Row], water: dict | None) -> Score:
    lv = lambda r: r.verdict.level   # noqa: E731
    first = R.first_at(rows, "caution")
    top = max((lv(r) for r in rows if lv(r) in J.LEVEL_RANK), key=J.LEVEL_RANK.__getitem__, default=J.UNKNOWN)
    max1h = max((r.verdict.rain.value for r in rows if r.verdict.rain.value is not None), default=None)
    sc = Score(day, max1h, first, top, R.first_at(rows, "caution", key=lambda r: r.rule_only))
    if water is None:
        return sc
    day_pts = sorted((t, v) for t, v in water.items() if v is not None and t.date() == day)
    if not day_pts:
        sc.verdict = "수위 자료 없음"
        return sc
    base = day_pts[0][1]
    sc.peak_at, peak = max(day_pts, key=lambda x: x[1])
    sc.rise = peak - base
    sc.with_water_first = R.first_at(rows, "caution", key=lambda r: r.with_water or J.UNKNOWN)
    hold = hold_series(water)
    sc.hold_first = next((t for t, h in sorted(hold.items()) if t.date() == day and h >= HOLD_M), None)
    warned = sc.with_water_first
    if sc.rise >= RISE_HIT_M:
        sc.verdict = "적중" if warned and warned <= sc.peak_at else "놓침"
    else:
        sc.verdict = "헛경보" if warned else "조용"
    return sc


# ---------------------------------------------------------------------------
# 자료 받기
# ---------------------------------------------------------------------------

def hourly_rain(code: str, years: list[int], client) -> dict[datetime, Decimal | None]:
    out: dict = {}
    for y in years:
        for m in MONTHS:
            s = datetime(y, m, 1, 1, tzinfo=KST)
            e = (datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=KST))
            try:
                out.update(R.fetch_1h(code, s, e, client=client))
            except Exception as ex:  # noqa: BLE001
                print(f"    {code} {y}-{m:02d} 1시간 자료 못 받음 ({type(ex).__name__})")
    return out


def event_stations(v, stations, n: int = 3) -> list[str]:
    """큰비 날을 고를 HRFCO 비 관측소 후보 (기상청 AWS 계곡은 가까운 HRFCO 순서로 n개 — 자료 없는 곳은 건너뜀)."""
    if v.rain_source == "hrfco":
        return [v.rain_code]
    from app import candidates as C

    rain = [s for s in stations if s.kind == "rainfall"]
    rain.sort(key=lambda s: C.km(float(v.lat), float(v.lon), s.lat, s.lon))
    return [s.code for s in rain[:n]]


def has_data(hourly: dict) -> bool:
    return any(x is not None for x in hourly.values())


def run_day(v, day: date, thresholds, model, client) -> tuple[list[R.Row], dict | None]:
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    end = start + timedelta(hours=23, minutes=50)
    if v.rain_source == "kma":
        from app.kma_aws import fetch_range

        series = fetch_range(v.rain_code, start - R.PAD, end, client=client)
        kind = J.KIND_RAIN_1H
    else:
        series = replay.fetch(v.rain_code, "rainfall", start - R.PAD, end, client=client)
        kind = J.KIND_RAIN_10M
    water = None
    if v.water_code:
        water = replay.fetch(v.water_code, "waterlevel", start - timedelta(hours=24), end, client=client)
    return R.run(series, kind, start, end, thresholds, model, water), water


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

def _t(x) -> str:
    return f"{x:%H:%M}" if x else "-"


def line(v, s: Score) -> str:
    base = (f"  {s.day}  최대1h {s.max_1h or 0:>5}mm  여울목 주의 {_t(s.first_caution):>5} (최고 {R.LEVEL_KO.get(s.top_level, s.top_level)})"
            f"  규칙만 {_t(s.rule_only_first):>5}")
    if v.water_code:
        base += (f"  | 실제 +{s.rise if s.rise is not None else '-'}m 최고 {_t(s.peak_at)}"
                 f"  비+수위 주의 {_t(s.with_water_first)}  평소대비 {_t(s.hold_first)}  → {s.verdict}")
    return base


def save(results, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["계곡", "분류", "날짜", "최대 1시간 강수(mm)", "여울목 주의", "여울목 최고 단계", "규칙만 주의",
                    "실제 상승(m)", "수위 최고 시각", "비+수위 주의", "평소대비 규칙(24h최저+0.3m)", "채점"])
        for v, s in results:
            w.writerow([v.name, "A" if v.water_code else "B", s.day, s.max_1h, _t(s.first_caution),
                        R.LEVEL_KO.get(s.top_level, s.top_level), _t(s.rule_only_first),
                        s.rise if s.rise is not None else "", _t(s.peak_at), _t(s.with_water_first),
                        _t(s.hold_first), s.verdict])


def main(argv=None) -> None:
    import logging

    import httpx

    from app import candidates as C
    from app import valleys as V

    logging.basicConfig(level="WARNING")
    p = argparse.ArgumentParser(prog="python -m app.backtest", description="계곡별 큰비 날 일괄 재현 (DB·푸시 안 건드림)")
    p.add_argument("--valley", help="계곡 이름 하나만")
    p.add_argument("--top", type=int, default=3, help="계곡마다 큰비 날 몇 번")
    p.add_argument("--years", default="2024,2025", help="쉼표로 (6~9월만 봄)")
    a = p.parse_args(argv)
    years = [int(y) for y in a.years.split(",")]

    valleys = [v for v in V.load_csv() if not a.valley or v.name == a.valley]
    model = est_mod.load_model()
    results = []
    with httpx.Client(timeout=30) as c:
        stations = C.all_stations(c) if any(v.rain_source == "kma" for v in valleys) else []
        hourly_cache: dict[str, dict] = {}
        for v in valleys:
            codes = event_stations(v, stations)
            code = ""
            for cand in codes:
                if cand not in hourly_cache:
                    hourly_cache[cand] = hourly_rain(cand, years, c)
                if has_data(hourly_cache[cand]):
                    code = cand
                    break
            kind = "A 비+수위" if v.water_code else "B 비만"
            print(f"\n■ {v.name} [{kind}]  비 {v.rain_source}:{v.rain_code}" + (f"  수위 {v.water_code}" if v.water_code else "")
                  + (f"  (큰비 날은 HRFCO {code}로 고름)" if code != v.rain_code else ""))
            if not code:
                print(f"  큰비 날을 고를 HRFCO 관측소 자료 없음 ({', '.join(codes) or '-'}) → 건너뜀")
                continue
            events = pick_events(hourly_cache[code], a.top)
            if not events:
                print(f"  {', '.join(map(str, years))}년 6~9월에 1시간 {MIN_EVENT_MM}mm 넘는 날 없음")
                continue
            thresholds = R.load_thresholds(v.name)
            for day, _ in events:
                try:
                    rows, water = run_day(v, day, thresholds, model, c)
                    s = score_day(day, rows, water)
                except Exception as ex:  # noqa: BLE001
                    print(f"  {day} 재현 실패 ({type(ex).__name__}: {ex})")
                    continue
                results.append((v, s))
                print(line(v, s))
    a_res = [s for v, s in results if v.water_code]
    if a_res:
        cnt = {k: sum(1 for s in a_res if s.verdict == k) for k in ("적중", "놓침", "헛경보", "조용", "수위 자료 없음")}
        print(f"\nA 계곡 채점 ({len(a_res)}번): " + " / ".join(f"{k} {n}" for k, n in cnt.items() if n))
    print(f"전체 재현 {len(results)}번 ({len({v.name for v, _ in results})}곳)")
    path = OUT / "summary.csv"
    save(results, path)
    print(f"CSV: {path}")


if __name__ == "__main__":
    main()
