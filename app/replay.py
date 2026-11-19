"""지난 비 오는 날 재생 — 시연·사용자 테스트용 (#26).

11월엔 경보가 안 떠서 판넬·발표·사용자 테스트에 쓸 '경보 뜨는 장면'이 없다.
지난 비 오는 날의 실제 10분 자료(HRFCO)를 받아, 시간만 지금으로 옮겨 시연 계곡에 흘려보낸다.

- 시연 계곡 '금대리 계곡 (시연)' + 재생 관측소 2개 (source='replay'). 진짜 금대리·구독자는 안 건드림
  강수: 서곡초교(10064070, 동부교 수위와 상관 0.94) / 수위: 동부교(1006664) — 노션 5단계 B·C
- 재생 중 매 단계: 관측값을 '지금' 기준으로 다시 써 넣고 → 판정 → AI 설명 → 푸시 (실제와 같은 코드)
- 평소 judge·collector는 시연 계곡을 건드리지 않음 (judge.ACTIVE_VALLEYS, collector의 replay 건너뜀)
- 시연 계곡을 구독한 폰에만 알림이 감. 밤(22~06시) 주의 무음 규칙도 그대로

  python -m app.replay setup                       # 시연 계곡 만들기 (여러 번 해도 같음)
  python -m app.replay run 8/22 --speed 10         # 8/22 비를 10배속으로 (10분 자료 1칸 = 1분)
  python -m app.replay run 7/8                     # 실제 속도 (10분마다 1칸)
  python -m app.replay stop [--hide]               # 시연 자료 지우기 (--hide: 앱 목록에서도 숨김)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

log = logging.getLogger("replay")

KST = ZoneInfo("Asia/Seoul")
STEP = timedelta(minutes=10)
WINDOW = timedelta(hours=3)          # 매 단계 다시 쓰는 범위 (judge LOOKBACK과 같음)
PAD = timedelta(hours=2)             # 재생 시작 전 자료 (1시간 강수·30분 상승을 바로 계산할 수 있게)
HRFCO = "https://api.hrfco.go.kr"

DEMO_NAME = "금대리 계곡 (시연)"
SOURCE_VALLEY = "금대리 계곡"
REPLAY_STATIONS = {   # code: (원본 HRFCO 코드, kind, 이름, 역할)
    "rain": ("10064070", "rainfall", "시연 강수 (서곡초교 재생)", "upstream_rain"),
    "water": ("1006664", "waterlevel", "시연 수위 (동부교 재생)", "downstream_water"),
}
VALUE_FIELD = {"rainfall": "rf", "waterlevel": "wl"}

# 노션 5단계 B·C에서 본 2026년 비. (시작, 끝) 한국시간
EVENTS = {
    "8/22": ("2026-08-22 02:00", "2026-08-22 08:00"),   # 20분 만에 +0.1 → +0.3m, 최고 +0.51m
    "7/8": ("2026-07-08 07:30", "2026-07-08 14:00"),    # 1시간 30mm, 최고 +0.62m
    "6/20": ("2026-06-20 03:00", "2026-06-20 13:00"),   # 약한 비가 오래 → 천천히 +0.34m
}


def parse_event(name: str) -> tuple[datetime, datetime]:
    if name not in EVENTS:
        raise SystemExit(f"모르는 날짜 {name}. 고를 수 있는 것: {', '.join(EVENTS)}")
    s, e = (datetime.strptime(x, "%Y-%m-%d %H:%M").replace(tzinfo=KST) for x in EVENTS[name])
    return s, e


def floor10(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 10, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# 원본 자료 받기 (HRFCO 10분)
# ---------------------------------------------------------------------------

def _dec(raw) -> Decimal | None:
    s = str(raw).strip() if raw is not None else ""
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_rows(payload: dict, kind: str) -> dict[datetime, Decimal | None]:
    out = {}
    for row in payload.get("content") or []:
        ymdhm = str(row.get("ymdhm", "")).strip()
        if len(ymdhm) == 12:
            out[datetime.strptime(ymdhm, "%Y%m%d%H%M").replace(tzinfo=KST)] = _dec(row.get(VALUE_FIELD[kind]))
    return out


def fetch(code: str, kind: str, start: datetime, end: datetime, client=None) -> dict[datetime, Decimal | None]:
    """하루씩 나눠 받음 (큰 응답은 끊길 수 있어서)."""
    own = client is None
    if own:
        import httpx

        client = httpx.Client(timeout=20)
    out: dict = {}
    try:
        day = start
        while day <= end:
            stop = min(day + timedelta(days=1) - STEP, end)
            url = (f"{HRFCO}/{os.environ['HRFCO_KEY']}/{kind}/list/10M/{code}/"
                   f"{day.strftime('%Y%m%d%H%M')}/{stop.strftime('%Y%m%d%H%M')}.json")
            r = client.get(url)
            r.raise_for_status()
            out.update(parse_rows(r.json(), kind))
            day = stop + STEP
    finally:
        if own:
            client.close()
    return out


# ---------------------------------------------------------------------------
# 시간 옮기기
# ---------------------------------------------------------------------------

def window(series: dict, pointer: datetime, anchor: datetime) -> list[tuple[datetime, Decimal | None]]:
    """재생 위치(pointer)까지의 자료를, pointer가 anchor(지금)에 오도록 시간만 옮긴다."""
    rows = []
    for k in range(int(WINDOW / STEP) + 1):
        src = pointer - STEP * k
        if src in series:
            rows.append((anchor - STEP * k, series[src]))
    return rows


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def setup(conn) -> int:
    """시연 계곡·재생 관측소·연결·기준값을 만든다. 이미 있으면 그대로 두고 id를 돌려준다."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM valleys WHERE name = %s", (DEMO_NAME,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE valleys SET is_active = TRUE WHERE id = %s", (row[0],))
            return row[0]
        cur.execute("SELECT id, region, lat, lon FROM valleys WHERE name = %s", (SOURCE_VALLEY,))
        src = cur.fetchone()
        if not src:
            raise SystemExit(f"'{SOURCE_VALLEY}'이 없어서 기준값을 복사할 수 없음")
        cur.execute("INSERT INTO valleys (name, region, lat, lon) VALUES (%s, %s, %s, %s) RETURNING id",
                    (DEMO_NAME, src[1], src[2], src[3]))
        demo = cur.fetchone()[0]
        for code, (_, kind, name, role) in REPLAY_STATIONS.items():
            cur.execute("SELECT id FROM stations WHERE source = 'replay' AND code = %s", (code,))
            st = cur.fetchone()
            if not st:
                cur.execute("INSERT INTO stations (source, code, name, kind) VALUES ('replay', %s, %s, %s) "
                            "RETURNING id", (code, name, kind))
                st = cur.fetchone()
            cur.execute("INSERT INTO valley_stations (valley_id, station_id, role) VALUES (%s, %s, %s)",
                        (demo, st[0], role))
        cur.execute("INSERT INTO valley_thresholds (valley_id, level, rain_1h_mm, water_rise_m) "
                    "SELECT %s, level, rain_1h_mm, water_rise_m FROM valley_thresholds WHERE valley_id = %s",
                    (demo, src[0]))
        return demo


def _station_ids(cur) -> dict[str, int]:
    cur.execute("SELECT code, id FROM stations WHERE source = 'replay'")
    return dict(cur.fetchall())


def reset(conn, demo: int) -> None:
    """재생 자료·판정·푸시 기록을 지운다 → 다음 재생에서 알림이 처음부터 다시 감."""
    with conn.cursor() as cur:
        ids = list(_station_ids(cur).values())
        cur.execute("DELETE FROM observations WHERE station_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM valley_status WHERE valley_id = %s", (demo,))
        cur.execute("DELETE FROM push_log WHERE valley_id = %s", (demo,))


def write_step(conn, series: dict[str, dict], pointer: datetime, anchor: datetime) -> None:
    with conn.transaction(), conn.cursor() as cur:
        ids = _station_ids(cur)
        for code, data in series.items():
            cur.execute("DELETE FROM observations WHERE station_id = %s", (ids[code],))
            for observed_at, value in window(data, pointer, anchor):
                cur.execute("INSERT INTO observations (station_id, observed_at, value) VALUES (%s, %s, %s)",
                            (ids[code], observed_at, value))


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------

def run(event: str, speed: float, sleep=time.sleep, clock=lambda: datetime.now(KST)) -> None:
    from app import judge

    start, end = parse_event(event)
    print(f"{event} 자료 받는 중 ({start:%m/%d %H:%M} ~ {end:%H:%M}, 앞 2시간 포함)…")
    series = {code: fetch(src, kind, start - PAD, end) for code, (src, kind, _, _) in REPLAY_STATIONS.items()}
    for code, data in series.items():
        print(f"  {REPLAY_STATIONS[code][2]}: {sum(v is not None for v in data.values())}칸")
    steps = int((end - start) / STEP) + 1
    print(f"재생: {steps}칸, {speed:g}배속 → 약 {steps * 10 / speed:.0f}분\n")

    with judge.connect() as conn:
        demo = setup(conn)
        reset(conn, demo)
        for i in range(steps):
            pointer = start + STEP * i
            now = clock()
            write_step(conn, series, pointer, floor10(now))
            v, changed = judge.process_valley(conn, demo, DEMO_NAME, now)
            rain = f"{v.rain.value}mm" if v.rain.value is not None else "-"
            rise = f"{v.rise.value}m" if v.rise.value is not None else "-"
            print(f"[원래 {pointer:%m/%d %H:%M}] {v.level:<8}{' ← 바뀜' if changed else ''}  강수1h {rain}  수위상승30m {rise}")
            if i < steps - 1:
                sleep(600 / speed)
    print("\n끝. 그대로 두면 30분 뒤 앱에선 '판정 불가'로 바뀜. 지우려면: python -m app.replay stop")


def stop(hide: bool) -> None:
    from app import judge

    with judge.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM valleys WHERE name = %s", (DEMO_NAME,))
            row = cur.fetchone()
        if not row:
            print("시연 계곡이 없음")
            return
        reset(conn, row[0])
        if hide:
            with conn.cursor() as cur:
                cur.execute("UPDATE valleys SET is_active = FALSE WHERE id = %s", (row[0],))
    print("시연 자료를 지웠어요" + (" (앱 목록에서도 숨김)" if hide else ""))


def main(argv=None) -> None:
    logging.basicConfig(level="WARNING", format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m app.replay", description="지난 비 오는 날 재생 (시연용)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="시연 계곡 만들기")
    r = sub.add_parser("run", help="재생")
    r.add_argument("event", choices=list(EVENTS))
    r.add_argument("--speed", type=float, default=1.0, help="배속 (10 = 10분 자료 1칸을 1분마다)")
    s = sub.add_parser("stop", help="시연 자료 지우기")
    s.add_argument("--hide", action="store_true", help="앱 목록에서도 숨기기")
    a = p.parse_args(argv)
    if a.cmd == "setup":
        from app import judge

        with judge.connect() as conn:
            print(f"시연 계곡 id = {setup(conn)} ('{DEMO_NAME}')")
    elif a.cmd == "run":
        if a.speed <= 0:
            sys.exit("--speed는 0보다 커야 해요")
        run(a.event, a.speed)
    else:
        stop(a.hide)


if __name__ == "__main__":
    main()
