"""재생(app/replay.py) 테스트 — DB·HRFCO 없이 가짜로.  실행: python tests/test_replay.py"""

import contextlib
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import replay as R  # noqa: E402
from app import valley_view as vv  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def t(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def test_events_and_floor():
    s, e = R.parse_event("8/22")
    assert s == t("2026-08-22 02:00") and e == t("2026-08-22 08:00")
    assert R.floor10(datetime(2026, 11, 20, 9, 37, 12, tzinfo=KST)) == t("2026-11-20 09:30")
    try:
        R.parse_event("1/1")
    except SystemExit:
        return
    raise AssertionError


def test_parse_rows_blank_is_none():
    payload = {"content": [{"ymdhm": "202608220500", "rf": "3.5"}, {"ymdhm": "202608220510", "rf": " "},
                           {"ymdhm": "bad", "rf": "1"}]}
    rows = R.parse_rows(payload, "rainfall")
    assert rows == {t("2026-08-22 05:00"): D("3.5"), t("2026-08-22 05:10"): None}


def test_window_moves_time_only():
    series = {t("2026-08-22 05:00") + R.STEP * i: D(i) for i in range(-30, 10)}
    pointer, anchor = t("2026-08-22 05:00"), t("2026-11-20 09:30")
    rows = window = R.window(series, pointer, anchor)
    assert rows[0] == (anchor, D(0))                                   # 재생 위치 = 지금
    assert rows[3] == (anchor - timedelta(minutes=30), D(-3))           # 30분 전 = 원래 30분 전
    assert len(window) == 19 and all(r[0] <= anchor for r in rows)      # 3시간, 미래 자료 없음


class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class _Client:
    def __init__(self):
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return _Resp({"content": [{"ymdhm": url.split("/")[-2], "wl": "0.20"}]})


def test_fetch_splits_by_day():
    os.environ["HRFCO_KEY"] = "K"
    c = _Client()
    out = R.fetch("1006664", "waterlevel", t("2026-08-21 22:00"), t("2026-08-22 08:00"), c)
    assert len(c.urls) == 1                                             # 하루 안이면 1번
    assert c.urls[0].startswith("https://api.hrfco.go.kr/K/waterlevel/list/10M/1006664/202608212200/")
    c = _Client()
    R.fetch("1006664", "waterlevel", t("2026-08-20 00:00"), t("2026-08-22 08:00"), c)
    assert len(c.urls) == 3 and out


class _Cur:
    def __init__(self, db):
        self.db, self.r = db, None

    def execute(self, sql, p=()):
        assert sql.count("%s") == len(p), sql
        self.db.log.append(sql.split()[0] + " " + sql.split()[2])
        if "FROM valleys WHERE name" in sql:
            self.r = [(1, "원주시 판부면", 37.2, 128.0)] if p[0] == R.SOURCE_VALLEY else (
                [(9,)] if self.db.demo_exists else [])
        elif sql.startswith("INSERT INTO valleys"):
            self.r = [(9,)]
        elif "FROM stations WHERE source = 'replay' AND code" in sql:
            self.r = []
        elif sql.startswith("INSERT INTO stations"):
            self.r = [(20 if p[0] == "rain" else 21,)]
        elif "SELECT code, id FROM stations" in sql:
            self.r = [("rain", 20), ("water", 21)]
        else:
            self.r = []

    def fetchone(self):
        return self.r[0] if self.r else None

    def fetchall(self):
        return self.r

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _Conn:
    def __init__(self, demo_exists=False):
        self.log, self.demo_exists = [], demo_exists

    def cursor(self):
        return _Cur(self)

    def transaction(self):
        return contextlib.nullcontext()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_setup_creates_once():
    c = _Conn()
    assert R.setup(c) == 9
    assert "INSERT valley_stations" in " ".join(c.log) and any("valley_thresholds" in x for x in c.log)
    c = _Conn(demo_exists=True)
    assert R.setup(c) == 9 and not any(x.startswith("INSERT") for x in c.log)       # 두 번째는 그대로


def test_run_end_to_end_with_fakes():
    from app import judge

    os.environ["HRFCO_KEY"] = "K"
    start, end = R.parse_event("8/22")
    fake = {"rain": {start - R.PAD + R.STEP * i: D("1.0") for i in range(60)},
            "water": {start - R.PAD + R.STEP * i: D("0.10") + D("0.01") * i for i in range(60)}}
    R.fetch = lambda code, kind, s, e, client=None: fake["rain" if kind == "rainfall" else "water"]
    conn = _Conn()
    calls, sleeps = [], []
    old_connect, old_process = judge.connect, judge.process_valley
    judge.connect = lambda: conn
    judge.process_valley = lambda c, vid, name, now: (calls.append((vid, now)) or (
        SimpleNamespace(level="caution", rain=SimpleNamespace(value=D("6.0")), rise=SimpleNamespace(value=D("0.03"))), False))
    try:
        R.run("8/22", 10, sleep=sleeps.append, clock=lambda: t("2026-11-20 09:37"))
    finally:
        judge.connect, judge.process_valley = old_connect, old_process
    assert len(calls) == 37 and all(v == 9 for v, _ in calls)          # 02:00~08:00 = 37칸, 시연 계곡만
    assert sleeps == [60.0] * 36                                        # 10배속 = 1분마다
    assert any(x.startswith("DELETE push_log") for x in conn.log)      # 시작할 때 알림 기록 초기화


def test_source_label():
    assert vv.source_text(["replay"]) == "자료: 한강홍수통제소 2026년 기록 재생(시연)"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
