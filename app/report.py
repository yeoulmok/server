"""운영보고서 AI 초안 (#21) — 기간을 넣으면 계곡별 운영 이력을 집계하고, 생성형 AI가 담당자용 초안 문장을 씀.

신청서 약속: "운영 이력을 바탕으로 시즌 보고서 초안을 AI가 작성, 담당자 검토".
경보 설명(app/explain.py)과 같은 원칙:
- **숫자는 서버가 계산**하고, AI는 그 숫자로 문장만 씀
- AI 글의 숫자가 넘겨준 사실에 없으면 버리고 1번 더, 그래도 틀리면 AI 없이 표만 남김
- "○분 뒤 도착" 같은 예측 금지

  python -m app.report                                  # 지난 7일
  python -m app.report --from 2026-11-17 --to 2026-11-24
  python -m app.report --no-ai                          # 표만 (API 안 부름)

결과: 화면 + data/reports/운영보고서_<시작>_<끝>.md  (담당자가 검토·수정해서 씀)
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app import explain

log = logging.getLogger("report")
KST = ZoneInfo("Asia/Seoul")
OUT = Path(__file__).resolve().parents[1] / "data" / "reports"
LEVELS = ("safe", "caution", "danger", "evacuate", "unknown")
KO = explain.LEVEL_KO
RANK = {"safe": 0, "caution": 1, "danger": 2, "evacuate": 3}
MAX_CHARS = 1500
STEP = timedelta(minutes=10)

SYSTEM = """너는 계곡 급류 경보 서비스 '여울목'의 운영 담당자를 돕는다. 아래 [사실]만 써서 운영보고서 초안을 한국어로 쓴다.
규칙:
- 숫자는 [사실]에 있는 것만 그대로 쓴다. 새로 계산하거나(합·평균·비율) 반올림하지 않는다.
- 앞으로의 위험·도착 시간·날씨를 예측하지 않는다. 원인을 지어내지 않는다.
- 형식: '## 요약'(3문장 이내), '## 계곡별 특이사항'(경보·판정 불가·자료 끊김이 있었던 계곡만, 한 줄씩),
  '## 담당자 확인 필요'(자료 끊김·판정 불가가 길었던 관측소나 계곡, 없으면 '없음')
- {max}자 이내. 마크다운 제목과 '- ' 목록만 쓴다."""


# ---------------------------------------------------------------------------
# 집계 (순수 계산)
# ---------------------------------------------------------------------------

@dataclass
class ValleyStats:
    name: str
    kind: str                                   # A 비+수위 / B 비만
    minutes: dict = field(default_factory=lambda: {k: 0 for k in LEVELS})
    top: str = "safe"
    changes: int = 0                            # 기간 안 단계 바뀐 횟수
    ai_stages: int = 0                          # AI 추정으로 정해진 단계 (경보)
    alerts: int = 0                             # 실제 발송된 경보 푸시
    recoveries: int = 0
    rain_max_1h: Decimal | None = None
    rise_max_30m: Decimal | None = None
    gaps: dict = field(default_factory=dict)    # 관측소 이름 → 자료 빠진 비율(%)


def stage_minutes(rows: list[tuple[datetime, str, bool]], start: datetime, end: datetime) -> tuple[dict, str, int, int]:
    """valley_status 행 [(changed_at, level, estimated)] (시간순, 시작 전 마지막 행 포함)
    → (단계별 분, 최고 단계, 기간 안 바뀐 횟수, AI 추정 경보 단계 수)"""
    mins = {k: 0 for k in LEVELS}
    top, changes, ai = "safe", 0, 0
    for i, (at, lv, est) in enumerate(rows):
        a = max(at, start)
        b = min(rows[i + 1][0] if i + 1 < len(rows) else end, end)
        if b <= a:
            continue
        lv_key = lv if lv in mins else "unknown"
        mins[lv_key] += int((b - a).total_seconds() // 60)
        if lv in RANK and RANK[lv] > RANK[top]:
            top = lv
        if at >= start:
            changes += 1
            if est and lv in RANK and RANK[lv] >= 1:
                ai += 1
    return mins, top, changes, ai


def max_rain_1h(series: dict[datetime, Decimal | None], kind: str) -> Decimal | None:
    """10분 자료면 1시간(6칸) 합의 최대, 1시간 누적 자료면 값의 최대."""
    pts = sorted((t, v) for t, v in series.items() if v is not None)
    if not pts:
        return None
    if kind == "rainfall_1h":
        return max(v for _, v in pts)
    vals = dict(pts)
    best = Decimal(0)
    for t, _ in pts:
        s = sum((vals.get(t - STEP * k) or Decimal(0)) for k in range(6))
        best = max(best, s)
    return best


def max_rise_30m(series: dict[datetime, Decimal | None]) -> Decimal | None:
    best = None
    for t, v in series.items():
        b = series.get(t - timedelta(minutes=30))
        if v is not None and b is not None:
            best = v - b if best is None else max(best, v - b)
    return best


def gap_pct(series: dict, start: datetime, end: datetime) -> int:
    expected = int((end - start) / STEP)
    got = sum(1 for t, v in series.items() if v is not None and start <= t < end)
    return max(0, round(100 * (expected - got) / expected)) if expected else 0


def facts_for(stats: list[ValleyStats], start: datetime, end: datetime) -> explain.Facts:
    f = explain.Facts("운영보고서", "safe")
    nums = f.numbers

    def n(v) -> str:
        s = f"{v}"
        nums.add(s)
        return s

    days = (end - start).days
    f.lines.append(f"기간: {start:%Y-%m-%d} ~ {(end - timedelta(minutes=1)):%Y-%m-%d} ({n(days)}일)")
    for d in (start, end - timedelta(minutes=1)):
        nums.update({str(d.year), str(d.month), str(d.day), f"{d.month:02d}", f"{d.day:02d}"})
    a = sum(1 for s in stats if s.kind.startswith("A"))
    f.lines.append(f"계곡 {n(len(stats))}곳 (비+수위 {n(a)}곳, 비만 {n(len(stats) - a)}곳)")
    tot_alerts = sum(s.alerts for s in stats)
    warned = [s for s in stats if RANK.get(s.top, 0) >= 1]
    f.lines.append(f"경보 푸시 발송 {n(tot_alerts)}번, 주의 이상이 한 번이라도 있었던 계곡 {n(len(warned))}곳")
    for s in stats:
        parts = [f"최고 단계 {KO[s.top]}"]
        for lv in ("caution", "danger", "evacuate", "unknown"):
            if s.minutes[lv]:
                parts.append(f"{KO[lv]} {n(s.minutes[lv])}분")
        if s.alerts:
            parts.append(f"경보 푸시 {n(s.alerts)}번")
        if s.ai_stages:
            parts.append(f"AI 추정으로 정한 경보 단계 {n(s.ai_stages)}번")
        if s.rain_max_1h is not None:
            parts.append(f"최대 1시간 강수 {n(f'{s.rain_max_1h:.1f}')}mm")
        if s.rise_max_30m is not None:
            parts.append(f"최대 30분 수위 상승 {n(f'{s.rise_max_30m:.2f}')}m")
        for st, pct in s.gaps.items():
            if pct >= 5:
                parts.append(f"{st} 자료 빠짐 {n(pct)}%")
        f.lines.append(f"- {s.name} [{s.kind}]: " + ", ".join(parts))
    return f


def table(stats: list[ValleyStats]) -> str:
    head = "| 계곡 | 분류 | 최고 단계 | 주의·위험·대피(분) | 판정 불가(분) | 경보 푸시 | AI 경보 | 최대 1시간 강수 | 최대 30분 상승 | 자료 빠짐 |\n|---|---|---|---|---|---|---|---|---|---|\n"
    rows = []
    for s in stats:
        warn = s.minutes["caution"] + s.minutes["danger"] + s.minutes["evacuate"]
        gaps = ", ".join(f"{k} {v}%" for k, v in s.gaps.items() if v >= 5) or "-"
        rows.append(f"| {s.name} | {s.kind} | {KO[s.top]} | {warn} | {s.minutes['unknown']} | {s.alerts} | {s.ai_stages} | "
                    f"{'-' if s.rain_max_1h is None else f'{s.rain_max_1h:.1f}mm'} | "
                    f"{'-' if s.rise_max_30m is None else f'{s.rise_max_30m:.2f}m'} | {gaps} |")
    return head + "\n".join(rows)


def check_draft(text: str, f: explain.Facts) -> str | None:
    if not text:
        return "빈 응답"
    if len(text) > MAX_CHARS:
        return f"너무 김 ({len(text)}자)"
    allowed = {explain._num(x) for x in f.numbers | explain.WINDOW_NUMBERS}
    for x in explain.NUMBER.findall(text):
        if explain._num(x) not in allowed:
            return f"사실에 없는 숫자 {x}"
    if explain.FORECAST.search(text):
        return "도착 시간 예측"
    return None


def ai_draft(f: explain.Facts, client=None) -> tuple[str | None, str | None]:
    if not explain.ready():
        return None, "ANTHROPIC_API_KEY 없음"
    reason = None
    for _ in range(explain.ATTEMPTS):
        try:
            text = explain.call_claude(SYSTEM.format(max=MAX_CHARS), "[사실]\n" + "\n".join(f.lines), client,
                                       max_tokens=1500)
        except Exception as e:  # noqa: BLE001
            return None, f"API 실패: {e}"
        reason = check_draft(text, f)
        if reason is None:
            return text, None
        log.info("운영보고서 초안 검사 실패 (%s)", reason)
    return None, reason


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def collect(conn, start: datetime, end: datetime) -> list[ValleyStats]:
    out = []
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM valleys WHERE is_active AND name NOT LIKE %s ORDER BY id", ("%(시연)%",))
        for vid, name in cur.fetchall():
            cur.execute("SELECT vs.role, s.id, s.name, s.kind FROM valley_stations vs JOIN stations s ON s.id = vs.station_id "
                        "WHERE vs.valley_id = %s", (vid,))
            links = cur.fetchall()
            has_water = any(r[0] == "downstream_water" for r in links)
            st = ValleyStats(name, "A 비+수위" if has_water else "B 비만")
            cur.execute("(SELECT changed_at, level, estimated FROM valley_status WHERE valley_id = %s AND changed_at < %s "
                        " ORDER BY changed_at DESC LIMIT 1) UNION ALL "
                        "(SELECT changed_at, level, estimated FROM valley_status WHERE valley_id = %s "
                        " AND changed_at >= %s AND changed_at < %s) ORDER BY 1", (vid, start, vid, start, end))
            st.minutes, st.top, st.changes, st.ai_stages = stage_minutes(cur.fetchall(), start, end)
            cur.execute("SELECT kind, count(*) FROM push_log WHERE valley_id = %s AND NOT dry_run "
                        "AND created_at >= %s AND created_at < %s GROUP BY kind", (vid, start, end))
            pk = dict(cur.fetchall())
            st.alerts, st.recoveries = pk.get("alert", 0), pk.get("recovery", 0)
            for role, sid, sname, kind in links:
                cur.execute("SELECT observed_at, value FROM observations WHERE station_id = %s "
                            "AND observed_at >= %s AND observed_at < %s", (sid, start - timedelta(hours=1), end))
                series = dict(cur.fetchall())
                if role == "upstream_rain":
                    m = max_rain_1h({t: v for t, v in series.items() if t >= start}, kind)
                    st.rain_max_1h = m if st.rain_max_1h is None or (m is not None and m > st.rain_max_1h) else st.rain_max_1h
                elif role == "downstream_water":
                    st.rise_max_30m = max_rise_30m(series)
                st.gaps[sname] = gap_pct(series, start, end)
            out.append(st)
    return out


def main(argv=None) -> None:
    from app import judge

    logging.basicConfig(level="WARNING")
    p = argparse.ArgumentParser(prog="python -m app.report", description="운영보고서 AI 초안 (담당자 검토용)")
    p.add_argument("--from", dest="start", help="YYYY-MM-DD (기본: 7일 전)")
    p.add_argument("--to", dest="end", help="YYYY-MM-DD, 이 날 00시 전까지 (기본: 오늘)")
    p.add_argument("--no-ai", action="store_true", help="AI 없이 표만")
    a = p.parse_args(argv)
    today = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromisoformat(a.end).replace(tzinfo=KST) if a.end else today
    start = datetime.fromisoformat(a.start).replace(tzinfo=KST) if a.start else end - timedelta(days=7)
    if end <= start:
        raise SystemExit("--to가 --from보다 뒤여야 함")

    with judge.connect() as conn:
        stats = collect(conn, start, end)
    f = facts_for(stats, start, end)
    draft, why = (None, "--no-ai") if a.no_ai else ai_draft(f)
    title = f"# 여울목 운영보고서 (초안) — {start:%Y-%m-%d} ~ {(end - timedelta(minutes=1)):%Y-%m-%d}"
    body = [title, "",
            "> 숫자는 서버가 운영 기록(판정·발송·관측)에서 계산함. 문장은 생성형 AI 초안이며 담당자가 검토해서 씀.", ""]
    if draft:
        body += [draft, ""]
    else:
        body += [f"_AI 초안 없음 ({why}). 아래 표와 사실을 보고 작성._", ""]
    body += ["## 계곡별 집계", "", table(stats), "", "## AI에 넘긴 사실", "", *f.lines, ""]
    text = "\n".join(body)
    print(text)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"운영보고서_{start:%Y%m%d}_{(end - timedelta(minutes=1)):%Y%m%d}.md"
    path.write_text(text, encoding="utf-8")
    print(f"\n저장: {path}")


if __name__ == "__main__":
    main()
