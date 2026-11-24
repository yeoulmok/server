"""사고 당일 재현 (#32) — "그날 여울목이 있었다면 몇 시간 전에 주의가 떴나".

2025.7.19 14:09쯤 서곡리 용수골 계곡 고립 사고. 서곡초교(10064070) 과거 강수를 HRFCO에서 받아
그날 10분마다 지금 판정 엔진과 같은 함수(judge.decide, estimate.estimate)로 판정한다.

- 매 시각에는 그 시각까지의 자료만 쓴다 (미래 자료가 섞이지 않게)
- 서곡리처럼 하류 수위가 없는 계곡 = 강수 규칙 + 예측 AI 2단 알람 (judge 맨 위 설명)
- DB·푸시는 건드리지 않음. 기준값만 DB에서 읽고, 못 읽으면 기본값(15/25/40mm)
- 10분 자료가 거의 없으면 1시간 자료로 대신 판정 (그때는 1시간 간격)
- 결과: 화면 표 + data/reconstruct/<날짜>_<관측소>.csv (판넬 그래프용)

  python -m app.reconstruct                                   # 서곡리 2025-07-19 14:09 (기본)
  python -m app.reconstruct --accident "2025-07-19 14:09" --hours 12 --after 2
  python -m app.reconstruct --valley 구룡사계곡 --source kma --station 591 --accident "2025-07-19 14:09"
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app import estimate as est_mod
from app import judge as J
from app import replay

log = logging.getLogger("reconstruct")

KST = ZoneInfo("Asia/Seoul")
STEP = timedelta(minutes=10)
HOUR = timedelta(hours=1)
PAD = timedelta(hours=73)          # AI 특징(72시간 강수)용 앞쪽 자료

DEFAULT_VALLEY = "서곡리 용수골 계곡"
DEFAULT_STATION = "10064070"       # 원주시(서곡초교)
DEFAULT_ACCIDENT = "2025-07-19 14:09"
DEFAULT_THRESHOLDS = [             # 금대리와 같은 값 (seed.sql)
    J.Threshold("caution", Decimal("15"), Decimal("0.10")),
    J.Threshold("danger", Decimal("25"), Decimal("0.20")),
    J.Threshold("evacuate", Decimal("40"), Decimal("0.30")),
]
MIN_10M_RATIO = 0.5                # 10분 자료가 이보다 적게 오면 1시간 자료로
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "reconstruct"

LEVEL_KO = {"safe": "안전", "caution": "주의", "danger": "위험", "evacuate": "대피", "unknown": "판정 불가"}
NO_WATER = J.Signal(None, None, "수위 연결 없음")


@dataclass(frozen=True)
class Row:
    at: datetime
    rain_step: Decimal | None      # 그 칸 강수 (10분 또는 1시간)
    verdict: J.Verdict
    rule_only: str                 # AI 없이 1시간 강수 규칙만 썼을 때 단계
    water: Decimal | None = None   # 실제 수위 (m) — --water 줬을 때
    rise_30m: Decimal | None = None
    with_water: str | None = None  # 비 + 실제 수위로 판정한 단계 (A 계곡 방식)


# ---------------------------------------------------------------------------
# 자료 받기
# ---------------------------------------------------------------------------

def parse_1h(payload: dict) -> dict[datetime, Decimal | None]:
    """HRFCO 1시간 자료. ymdhm이 10자리(YYYYMMDDHH) 또는 12자리 모두 받음."""
    out = {}
    for row in payload.get("content") or []:
        s = str(row.get("ymdhm", "")).strip()
        if len(s) == 10:
            s += "00"
        if len(s) == 12:
            out[datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=KST)] = replay._dec(row.get("rf"))
    return out


def fetch_1h(code: str, start: datetime, end: datetime, client=None) -> dict[datetime, Decimal | None]:
    own = client is None
    if own:
        import httpx

        client = httpx.Client(timeout=30)
    try:
        url = (f"{replay.HRFCO}/{os.environ['HRFCO_KEY']}/rainfall/list/1H/{code}/"
               f"{start.strftime('%Y%m%d%H%M')}/{end.strftime('%Y%m%d%H%M')}.json")
        r = client.get(url)
        r.raise_for_status()
        return parse_1h(r.json())
    finally:
        if own:
            client.close()


def load_series(code: str, start: datetime, end: datetime,
                fetch10=replay.fetch, fetch1h=fetch_1h) -> tuple[str, dict]:
    """(kind, series). 10분 자료를 먼저 받고, 너무 비었으면 1시간 자료."""
    expected = int((end - start) / STEP) + 1
    try:
        s10 = fetch10(code, "rainfall", start, end)
    except Exception as e:  # noqa: BLE001 — 10분 자료가 없으면 1시간으로 넘어감
        print(f"  10분 자료 실패: {type(e).__name__} {e}")
        s10 = {}
    got = sum(v is not None for v in s10.values())
    print(f"  10분 자료: {got}/{expected}칸")
    if got >= expected * MIN_10M_RATIO:
        return J.KIND_RAIN_10M, s10
    s1h = fetch1h(code, start, end)
    print(f"  → 1시간 자료로: {sum(v is not None for v in s1h.values())}칸")
    return J.KIND_RAIN_1H, s1h


def load_kma(code: str, start: datetime, end: datetime, fetch=None) -> tuple[str, dict]:
    """기상청 AWS (1시간 누적 강수를 10분마다)."""
    if fetch is None:
        from app.kma_aws import fetch_range as fetch
    s = fetch(code, start, end)
    expected = int((end - start) / STEP) + 1
    print(f"  기상청 AWS {code}: {sum(v is not None for v in s.values())}/{expected}칸")
    return J.KIND_RAIN_1H, s


def load_thresholds(valley: str) -> list[J.Threshold]:
    try:
        with J.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT t.level, t.rain_1h_mm, t.water_rise_m FROM valley_thresholds t "
                        "JOIN valleys v ON v.id = t.valley_id WHERE v.name = %s", (valley,))
            rows = [J.Threshold(*r) for r in cur.fetchall()]
        if rows:
            return rows
        print(f"  '{valley}' 기준값이 DB에 없음 → 기본값")
    except Exception as e:  # noqa: BLE001
        print(f"  DB 못 읽음 ({type(e).__name__}) → 기본값")
    return DEFAULT_THRESHOLDS


# ---------------------------------------------------------------------------
# 판정 (순수 계산)
# ---------------------------------------------------------------------------

def judge_at(series: dict, kind: str, t: datetime, thresholds, model) -> tuple[J.Verdict, str]:
    """t 시각에 그 시각까지의 자료만으로 판정. (판정, 규칙만 썼을 때 단계)"""
    past = {k: v for k, v in series.items() if k <= t}
    if kind == J.KIND_RAIN_10M:
        rain, per_hour = J.rain_1h(past, t), est_mod.hourly_from_10min
    else:
        rain, per_hour = J.rain_1h_direct(past, t), est_mod.hourly_from_1h
    est = est_mod.estimate(past, t, per_hour, model)
    v = J.decide(rain, NO_WATER, thresholds, est, has_water=False)
    rule_only = J.level_for(rain.value, thresholds, "rain_1h_mm") if rain.value is not None else J.UNKNOWN
    return v, rule_only


def with_water_at(series: dict, kind: str, water: dict, t: datetime, thresholds, model) -> tuple[str, J.Signal]:
    """비 + 실제 수위(30분 상승)로 판정 — 지금 A 계곡(금대리 등)과 같은 방식. 수위가 비면 AI가 대신."""
    past = {k: v for k, v in series.items() if k <= t}
    if kind == J.KIND_RAIN_10M:
        rain, per_hour = J.rain_1h(past, t), est_mod.hourly_from_10min
    else:
        rain, per_hour = J.rain_1h_direct(past, t), est_mod.hourly_from_1h
    rise = J.water_rise_30m({k: v for k, v in water.items() if k <= t}, t)
    est = est_mod.estimate(past, t, per_hour, model) if rise.value is None else None
    return J.decide(rain, rise, thresholds, est, has_water=True).level, rise


def run(series: dict, kind: str, start: datetime, end: datetime, thresholds, model, water: dict | None = None) -> list[Row]:
    # 10분 간격 자료(HRFCO 10분, 기상청 AWS 1시간 누적을 10분마다)면 10분마다, HRFCO 1시간 자료면 1시간마다
    step = STEP if kind == J.KIND_RAIN_10M or any(k.minute for k in series) else HOUR
    rows, t = [], start
    while t <= end:
        v, rule_only = judge_at(series, kind, t, thresholds, model)
        row = Row(t, series.get(t), v, rule_only)
        if water is not None:
            lv, rise = with_water_at(series, kind, water, t, thresholds, model)
            row = Row(t, series.get(t), v, rule_only, water.get(t), rise.value, lv)
        rows.append(row)
        t += step
    return rows


def water_summary(rows: list[Row], accident: datetime) -> list[str]:
    """실제 수위가 얼마나 올랐나 + AI 추정과 비교 + 비+수위 판정."""
    w = [(r.at, r.water) for r in rows if r.water is not None]
    if not w:
        return ["실제 수위: 자료 없음"]
    base_at, base = w[0]
    top_at, top = max(w, key=lambda x: x[1])
    at_acc = next((x for x in reversed(w) if x[0] <= accident), None)
    out = [f"실제 수위 ({base_at:%H:%M} {base:.2f}m 기준)",
           f"  최고 {top:.2f}m ({top_at:%H:%M}, +{top - base:.2f}m)"]
    if at_acc:
        out.append(f"  사고 시각 근처 {at_acc[1]:.2f}m ({at_acc[0]:%H:%M}, +{at_acc[1] - base:.2f}m)")
        r = next((r for r in reversed(rows) if r.at <= accident), None)
        if r and r.verdict.est and r.verdict.est.rise is not None:
            out.append(f"  비교) 그 시각 AI 추정 상승 {r.verdict.est.rise:.2f}m ↔ 실제 +{at_acc[1] - base:.2f}m")
    first = first_at(rows, "caution", key=lambda r: r.with_water or J.UNKNOWN)
    out.append(f"비+실제 수위 판정: 주의 첫 발령 {first:%H:%M} ({lead(first, accident)})" if first
               else "비+실제 수위 판정: 주의 안 뜸")
    r = next((r for r in reversed(rows) if r.at <= accident), None)
    if r and r.with_water:
        out.append(f"  사고 순간 단계 {LEVEL_KO.get(r.with_water, r.with_water)}")
    return out


def first_at(rows: list[Row], level: str, key=lambda r: r.verdict.level) -> datetime | None:
    """이 단계 '이상'이 처음 나온 시각."""
    rank = J.LEVEL_RANK[level]
    for r in rows:
        lv = key(r)
        if lv in J.LEVEL_RANK and J.LEVEL_RANK[lv] >= rank:
            return r.at
    return None


def lead(at: datetime | None, accident: datetime) -> str:
    if at is None:
        return "안 뜸"
    d = accident - at
    if d < timedelta(0):
        m = int(-d.total_seconds() // 60)
        return f"사고 {m // 60}시간 {m % 60}분 뒤"
    m = int(d.total_seconds() // 60)
    return f"사고 {m // 60}시간 {m % 60}분 전"


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

def _n(x, nd=1) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def print_rows(rows: list[Row], accident: datetime) -> None:
    prev = None
    marked = False
    for r in rows:
        v, e = r.verdict, r.verdict.est
        if not marked and r.at >= accident:
            print(f"---- {accident:%H:%M} 사고 ----")
            marked = True
        flag = " ← 바뀜" if prev is not None and v.level != prev else ""
        ai = " (AI)" if v.estimated else ""
        print(f"{r.at:%m/%d %H:%M}  {LEVEL_KO.get(v.level, v.level):<5}{ai:<5}"
              f" 강수1h {_n(v.rain.value):>5}mm  24h {_n(e.rain_24h if e else None):>5}mm"
              f"  AI추정 {_n(e.rise if e else None, 2):>5}m  규칙만 {LEVEL_KO.get(r.rule_only, r.rule_only)}"
              + (f"  수위 {_n(r.water, 2)}m 30분 {_n(r.rise_30m, 2)}m → {LEVEL_KO.get(r.with_water, r.with_water)}"
                 if r.with_water else "") + flag)
        prev = v.level


def summary(rows: list[Row], accident: datetime) -> list[str]:
    lines = [f"사고 시각 {accident:%Y-%m-%d %H:%M}"]
    for level in ("caution", "danger", "evacuate"):
        at = first_at(rows, level)
        lines.append(f"  {LEVEL_KO[level]} 첫 발령: {at:%H:%M} ({lead(at, accident)})" if at
                     else f"  {LEVEL_KO[level]}: {lead(None, accident)}")
    rule = first_at(rows, "caution", key=lambda r: r.rule_only)
    ai = first_at(rows, "caution")
    lines.append(f"  비교) AI 없이 1시간 강수 규칙만: 주의 {rule:%H:%M} ({lead(rule, accident)})" if rule
                 else "  비교) AI 없이 1시간 강수 규칙만: 주의 안 뜸")
    if ai and (rule is None or ai < rule):
        gain = (rule - ai) if rule else None
        lines.append("  → 예측 AI 덕분에 " + (f"{int(gain.total_seconds() // 60)}분 먼저" if gain else "규칙만으론 못 잡은 주의를") + " 알림")
    lines += at_accident(rows, accident)
    return lines


def _dur(d: timedelta) -> str:
    m = int(d.total_seconds() // 60)
    return f"{m // 60}시간 {m % 60}분"


def at_accident(rows: list[Row], accident: datetime) -> list[str]:
    """사고 순간(직전 판정)에 뭐라고 하고 있었나 — '처음 뜬 시각'보다 이게 진짜 차이 (#40)."""
    before = [r for r in rows if r.at <= accident]
    if not before:
        return []
    caution = J.LEVEL_RANK["caution"]
    out = [f"사고 순간 ({before[-1].at:%H:%M} 판정)"]
    for label, key, ai in (("여울목(AI 포함)", lambda r: r.verdict.level, before[-1].verdict.estimated),
                           ("규칙만", lambda r: r.rule_only, False)):
        alert = J.LEVEL_RANK.get(key(before[-1]), -1) >= caution
        since = before[-1].at                    # 지금 상태(주의 이상 / 아래)가 언제부터 이어졌나
        for r in reversed(before):
            if (J.LEVEL_RANK.get(key(r), -1) >= caution) != alert:
                break
            since = r.at
        lv = LEVEL_KO.get(key(before[-1]), key(before[-1])) + (" (AI)" if ai else "")
        state = "주의 이상" if alert else "주의 아래"
        out.append(f"  {label}: {lv} — {since:%H:%M}부터 {state} 유지 ({_dur(accident - since)}째)")
    return out


def save_csv(rows: list[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # 엑셀에서 한글 안 깨지게
        w = csv.writer(f)
        w.writerow(["시각", "칸 강수(mm)", "1시간 강수(mm)", "24시간 강수(mm)", "AI 추정 상승(m)",
                    "단계", "AI로 올린 단계", "규칙만 단계", "실제 수위(m)", "수위 30분 상승(m)", "비+수위 단계"])
        for r in rows:
            v, e = r.verdict, r.verdict.est
            w.writerow([f"{r.at:%Y-%m-%d %H:%M}", r.rain_step, v.rain.value,
                        e.rain_24h if e else None, e.rise if e else None,
                        LEVEL_KO.get(v.level, v.level), "예" if v.estimated else "",
                        LEVEL_KO.get(r.rule_only, r.rule_only), r.water, r.rise_30m,
                        LEVEL_KO.get(r.with_water, r.with_water) if r.with_water else ""])


def main(argv=None) -> None:
    logging.basicConfig(level="WARNING", format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m app.reconstruct", description="사고 당일 재현 (DB·푸시 안 건드림)")
    p.add_argument("--accident", default=DEFAULT_ACCIDENT, help='사고 시각 "YYYY-MM-DD HH:MM" (한국시간)')
    p.add_argument("--valley", default=DEFAULT_VALLEY, help="기준값을 읽을 계곡 이름")
    p.add_argument("--station", default=DEFAULT_STATION, help="강수 관측소 코드 (HRFCO 또는 기상청 AWS 번호)")
    p.add_argument("--source", choices=("hrfco", "kma"), default="hrfco", help="kma = 기상청 AWS (예: 591 치악산)")
    p.add_argument("--hours", type=float, default=12, help="사고 몇 시간 전부터 볼지")
    p.add_argument("--after", type=float, default=2, help="사고 뒤 몇 시간까지 볼지")
    p.add_argument("--water", help="HRFCO 수위 관측소 코드 (예: 1006685 봉미교) → 실제 수위와 비+수위 판정도")
    a = p.parse_args(argv)

    accident = datetime.strptime(a.accident, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    start = replay.floor10(accident - timedelta(hours=a.hours))
    end = replay.floor10(accident + timedelta(hours=a.after))
    print(f"{a.valley} / 관측소 {a.station} / {start:%m/%d %H:%M} ~ {end:%m/%d %H:%M}")
    if a.source == "kma":
        kind, series = load_kma(a.station, start - PAD, end)
    else:
        kind, series = load_series(a.station, start - PAD, end)
    if not any(v is not None for v in series.values()):
        raise SystemExit("강수 자료를 하나도 못 받음 (관측소 코드·날짜 확인)")
    if kind == J.KIND_RAIN_1H and a.source == "hrfco":
        start = start.replace(minute=0)
    thresholds = load_thresholds(a.valley)
    model = est_mod.load_model()
    print(f"  기준값: {', '.join(f'{LEVEL_KO[t.level]} {t.rain_1h_mm}mm' for t in thresholds if t.rain_1h_mm)}"
          f" / AI 모델: {'있음' if model else '없음 (24시간 규칙만)'}\n")

    water = None
    if a.water:
        water = replay.fetch(a.water, "waterlevel", start - timedelta(hours=1), end)
        print(f"  수위 {a.water}: {sum(v is not None for v in water.values())}/{len(water)}칸")
    rows = run(series, kind, start, end, thresholds, model, water)
    print_rows(rows, accident)
    print()
    lines = summary(rows, accident) + (water_summary(rows, accident) if water is not None else [])
    print("\n".join(lines))
    path = OUT_DIR / f"{accident:%Y-%m-%d}_{a.source}{a.station}{'_w' + a.water if a.water else ''}.csv"
    save_csv(rows, path)
    print(f"\nCSV: {path}")


if __name__ == "__main__":
    main()
