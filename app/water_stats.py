"""계곡별 수위 기준값 근거 (#20) — 비+수위(A) 계곡마다 과거 여름 수위로 "평소 출렁임"과 "큰비 때 오름"을 보고
지금 기준(30분 상승 0.10/0.20/0.30m, 붙잡기 0.30m)이 맞는지, 계곡별로 바꿀지 제안한다. DB는 안 바꿈.

  python -u -m app.water_stats                     # A 계곡 전부, 2024·2025년 6~9월
  python -u -m app.water_stats --valley 섬강유원지
  python -u -m app.water_stats --years 2025        # 빨리 보기

10분 수위를 하루씩 받아 data/water_stats/raw/에 저장 → 다시 돌리면 받은 날은 건너뜀 (처음엔 계곡당 몇 분).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app import backtest as B
from app import judge as J
from app import replay

KST = B.KST
STEP = J.STEP
RAW = Path("data/water_stats/raw")
OUT = Path("data/water_stats/summary.csv")
DRY_HOURS = 6                  # 직전 6시간 비 0 → "비 없을 때"
EPISODE_GAP = timedelta(hours=6)
STORM_DAYS = 5                 # 계곡마다 큰비 날 몇 번 볼지
LEVELS = ("caution", "danger", "evacuate")
CURRENT = {"caution": Decimal("0.10"), "danger": Decimal("0.20"), "evacuate": Decimal("0.30")}
HOLD = J.HOLD_M


# ---------------------------------------------------------------------------
# 계산 (DB·네트워크 없이 테스트)
# ---------------------------------------------------------------------------

def rises_30m(series: dict[datetime, Decimal | None]) -> dict[datetime, Decimal]:
    out = {}
    for t, v in series.items():
        b = series.get(t - timedelta(minutes=30))
        if v is not None and b is not None:
            out[t] = v - b
    return out


def is_dry(t: datetime, hourly: dict[datetime, Decimal | None], hours: int = DRY_HOURS) -> bool:
    """t 직전 hours시간 1시간 강수가 전부 있고 전부 0."""
    top = t.replace(minute=0, second=0, microsecond=0) + (timedelta(hours=1) if t.minute else timedelta(0))
    vals = [hourly.get(top - timedelta(hours=k)) for k in range(hours + 1)]
    return all(v is not None and v == 0 for v in vals)


def percentile(xs: list[Decimal], p: float) -> Decimal | None:
    if not xs:
        return None
    s = sorted(xs)
    k = min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))
    return s[k]


def episodes(times: list[datetime], gap: timedelta = EPISODE_GAP) -> list[datetime]:
    """가까운 시각끼리 묶어 첫 시각만 (6시간 안에 또 넘으면 같은 번)."""
    out: list[datetime] = []
    last = None
    for t in sorted(times):
        if last is None or t - last > gap:
            out.append(t)
        last = t
    return out


def hold_values(series: dict[datetime, Decimal | None], window: timedelta = J.HOLD_WINDOW) -> dict[datetime, Decimal]:
    """t마다 수위 - 직전 24시간(그 시각 포함) 최저. 두 달치도 빠르게 (최솟값 큐)."""
    from collections import deque

    pts = sorted((t, v) for t, v in series.items() if v is not None)
    q: deque = deque()
    out = {}
    for t, v in pts:
        while q and q[-1][1] >= v:
            q.pop()
        q.append((t, v))
        while q[0][0] < t - window:
            q.popleft()
        out[t] = v - q[0][1]
    return out


@dataclass
class Stats:
    name: str
    water: str
    days: int = 0                                   # 수위 자료가 있는 날
    noise_999: Decimal | None = None                # 비 없을 때 30분 상승 99.9% (평소 출렁임 상한)
    dry_points: int = 0
    storm_max30: list[Decimal] = field(default_factory=list)   # 큰비 날마다 최대 30분 상승
    storm_hold: list[Decimal] = field(default_factory=list)    # 큰비 날마다 최대 (수위 - 24h 최저)
    fires: dict[str, tuple[int, int]] = field(default_factory=dict)  # 기준별 (전체 번, 비 없을 때 번)
    hold_fires: tuple[int, int] = (0, 0)
    suggest: dict[str, Decimal] = field(default_factory=dict)
    note: str = ""


def count_fires(rises: dict[datetime, Decimal], thr: Decimal, dry_of) -> tuple[int, int]:
    eps = episodes([t for t, r in rises.items() if r >= thr])
    return len(eps), sum(1 for t in eps if dry_of(t))


def round_up(x: Decimal, step: Decimal = Decimal("0.05")) -> Decimal:
    return (x / step).to_integral_value(rounding="ROUND_CEILING") * step


def round_down(x: Decimal, step: Decimal = Decimal("0.05")) -> Decimal:
    return (x / step).to_integral_value(rounding="ROUND_FLOOR") * step


def suggest(noise: Decimal | None, storm_max30: list[Decimal]) -> tuple[dict[str, Decimal], str]:
    """주의 = 평소 출렁임의 2배보다 크고(헛경보 막기), 큰비 날 절반 이상에서 넘을 만큼 작게(놓침 막기).
    위험·대피 = 주의의 2배·3배 (지금 0.1/0.2/0.3과 같은 비율). 0.05m 단위."""
    lo = round_up(noise * 2) if noise is not None else Decimal("0.05")
    lo = max(lo, Decimal("0.05"))
    med = percentile(storm_max30, 50) if storm_max30 else None
    hi = round_down(med) if med is not None else None
    cur = CURRENT["caution"]
    if hi is not None and hi < lo:
        c, why = lo, f"큰비 날 오름({med:.2f}m)이 평소 출렁임 2배({lo:.2f}m)보다 작음 → 수위로 가려내기 어려움, 비 규칙 위주"
    elif lo > cur:
        c, why = lo, f"평소 출렁임이 커서 {cur}m면 비 없이도 울릴 수 있음 → 올림"
    elif hi is not None and hi < cur:
        c, why = max(lo, hi), f"큰비 날에도 30분에 {med:.2f}m 정도만 오름 → {cur}m면 못 울림, 내림"
    else:
        c, why = cur, "지금 기준이 맞음"
    return {"caution": c, "danger": c * 2, "evacuate": c * 3}, why


# ---------------------------------------------------------------------------
# 자료 받기 (하루씩 저장)
# ---------------------------------------------------------------------------

def water_day(code: str, day: date, client) -> dict[datetime, Decimal | None]:
    p = RAW / code / f"{day:%Y%m%d}.json"
    if p.exists():
        return {datetime.fromisoformat(t): (None if v is None else Decimal(v)) for t, v in json.loads(p.read_text())}
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    got = replay.fetch(code, "waterlevel", start, start + timedelta(hours=23, minutes=50), client=client)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([[t.isoformat(), None if v is None else str(v)] for t, v in sorted(got.items())]))
    return got


def water_range(code: str, years: list[int], client) -> dict[datetime, Decimal | None]:
    out: dict = {}
    fails = 0
    for y in years:
        d = date(y, B.MONTHS[0], 1)
        while d <= date(y, B.MONTHS[-1], 30):
            try:
                out.update(water_day(code, d, client))
            except Exception:  # noqa: BLE001
                fails += 1
            d += timedelta(days=1)
    if fails:
        print(f"    수위 {code} 못 받은 날 {fails}일 (다시 돌리면 그날만 다시 받음)")
    return out


# ---------------------------------------------------------------------------
# 계곡 하나
# ---------------------------------------------------------------------------

def analyze(name: str, water_code: str, water: dict, hourly: dict) -> Stats:
    st = Stats(name, water_code)
    st.days = len({t.date() for t, v in water.items() if v is not None})
    rises = rises_30m(water)
    dry = [r for t, r in rises.items() if is_dry(t, hourly)]
    st.dry_points = len(dry)
    st.noise_999 = percentile(dry, 99.9)
    dry_of = lambda t: is_dry(t, hourly)  # noqa: E731

    holds = hold_values(water)
    for d, _ in B.pick_events(hourly, STORM_DAYS):
        s = datetime(d.year, d.month, d.day, tzinfo=KST)
        e = s + timedelta(days=1, hours=6)                 # 비 그친 뒤 물이 늦게 오는 것까지
        day_r = [r for t, r in rises.items() if s <= t < e]
        day_h = [h for t, h in holds.items() if s <= t < e]
        if day_r:
            st.storm_max30.append(max(day_r))
        if day_h:
            st.storm_hold.append(max(day_h))

    for lv in LEVELS:
        st.fires[lv] = count_fires(rises, CURRENT[lv], dry_of)
    eps = episodes([t for t, h in holds.items() if h >= HOLD])
    st.hold_fires = (len(eps), sum(1 for t in eps if dry_of(t)))
    st.suggest, st.note = suggest(st.noise_999, st.storm_max30)
    if st.days < 30:
        st.note = f"수위 자료 {st.days}일뿐 → 근거 약함. " + st.note
    return st


def fmt(x) -> str:
    return "-" if x is None else f"{x:.2f}"


def show(st: Stats) -> None:
    print(f"\n■ {st.name}  수위 {st.water}  (자료 {st.days}일, 비 없을 때 {st.dry_points}칸)")
    print(f"  평소 출렁임(비 없을 때 30분 상승 99.9%) {fmt(st.noise_999)}m")
    print(f"  큰비 날 {len(st.storm_max30)}번 최대 30분 상승  " + ", ".join(fmt(x) for x in st.storm_max30)
          + f"  (가운데 {fmt(percentile(st.storm_max30, 50))}m)")
    print(f"  큰비 날 최대 (수위 - 24h 최저)          " + ", ".join(fmt(x) for x in st.storm_hold))
    cur = " / ".join(f"{CURRENT[lv]}m {st.fires[lv][0]}번(비 없을 때 {st.fires[lv][1]})" for lv in LEVELS)
    print(f"  지금 기준으로 울린 횟수  {cur}  | 붙잡기 {HOLD}m {st.hold_fires[0]}번(비 없을 때 {st.hold_fires[1]})")
    sg = " / ".join(fmt(st.suggest[lv]) for lv in LEVELS)
    print(f"  → 제안 주의/위험/대피 {sg}m : {st.note}")


def save(rows: list[Stats]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["계곡", "수위", "자료일수", "평소출렁임99.9", "큰비30분최대_가운데", "큰비붙잡기최대",
                    "주의0.10_번", "주의_비없을때", "붙잡기0.30_번", "붙잡기_비없을때",
                    "제안_주의", "제안_위험", "제안_대피", "메모"])
        for s in rows:
            w.writerow([s.name, s.water, s.days, fmt(s.noise_999), fmt(percentile(s.storm_max30, 50)),
                        fmt(max(s.storm_hold) if s.storm_hold else None),
                        s.fires["caution"][0], s.fires["caution"][1], s.hold_fires[0], s.hold_fires[1],
                        fmt(s.suggest["caution"]), fmt(s.suggest["danger"]), fmt(s.suggest["evacuate"]), s.note])


def main(argv=None) -> None:
    import httpx

    from app import candidates as C
    from app import valleys as V

    p = argparse.ArgumentParser(prog="python -m app.water_stats", description="계곡별 수위 기준값 근거 (DB 안 바꿈)")
    p.add_argument("--valley")
    p.add_argument("--years", default="2024,2025")
    a = p.parse_args(argv)
    years = [int(y) for y in a.years.split(",")]
    vs = [v for v in V.load_csv() if v.water_code and (not a.valley or a.valley in v.name)]
    rows = []
    with httpx.Client(timeout=30) as c:
        stations = C.all_stations(c) if any(v.rain_source == "kma" for v in vs) else []
        rain_cache: dict[str, dict] = {}
        for v in vs:
            print(f"\n… {v.name}: 수위 {v.water_code} 받는 중", flush=True)
            hourly = {}
            for code in B.event_stations(v, stations):
                if code not in rain_cache:
                    rain_cache[code] = B.hourly_rain(code, years, c)
                if B.has_data(rain_cache[code]):
                    hourly = rain_cache[code]
                    break
            water = water_range(v.water_code, years, c)
            st = analyze(v.name, v.water_code, water, hourly)
            show(st)
            rows.append(st)
    save(rows)
    print(f"\n표: {OUT}")


if __name__ == "__main__":
    main()
