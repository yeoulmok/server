"""일괄 재현(app/backtest.py) — 큰비 날 고르기·채점만 (API 없이).  실행: python tests/test_backtest.py"""

import sys
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import backtest as B  # noqa: E402
from app import reconstruct as R  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def t(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def test_pick_events_top_gap_and_midnight():
    h = {t("2025-07-19 05:00"): D("22"), t("2025-07-20 03:00"): D("30"),      # 하루 차이 → 큰 것만
         t("2025-08-01 00:00"): D("15"),                                     # 00시 값 = 7/31 비
         t("2025-08-10 12:00"): D("9"), t("2025-06-01 12:00"): None}
    ev = B.pick_events(h, top=5)
    assert ev == [(date(2025, 7, 20), D("30")), (date(2025, 7, 31), D("15"))], ev


def test_hold_series():
    w = {t("2025-07-19 00:00"): D("1.0"), t("2025-07-19 06:00"): D("1.5"), t("2025-07-19 12:00"): D("1.3")}
    h = B.hold_series(w)
    assert h[t("2025-07-19 12:00")] == D("0.3") and h[t("2025-07-19 00:00")] == D("0")


def _rows(day, levels_ww):
    """(시각, 여울목 단계, 비+수위 단계) → Row 비슷한 것"""
    rows = []
    for hm, lv, ww in levels_ww:
        v = R.J.Verdict(lv, R.J.Signal(D("5"), None), R.J.Signal(None, None))
        rows.append(R.Row(t(f"{day} {hm}"), None, v, "safe", None, None, ww))
    return rows


def test_score_hit_miss_false_alarm():
    day = "2025-07-19"
    water = {t(f"{day} 00:00"): D("1.0"), t(f"{day} 08:00"): D("1.4"), t(f"{day} 12:00"): D("1.2")}
    rows = _rows(day, [("04:00", "caution", "caution"), ("10:00", "safe", "safe")])
    s = B.score_day(date(2025, 7, 19), rows, water)
    assert s.rise == D("0.4") and s.verdict == "적중" and s.peak_at == t(f"{day} 08:00")
    s2 = B.score_day(date(2025, 7, 19), _rows(day, [("09:00", "caution", "caution")]), water)
    assert s2.verdict == "놓침"                                  # 최고점(08:00) 뒤에 경고
    flat = {t(f"{day} 00:00"): D("1.0"), t(f"{day} 08:00"): D("1.05")}
    assert B.score_day(date(2025, 7, 19), _rows(day, [("04:00", "caution", "caution")]), flat).verdict == "헛경보"
    assert B.score_day(date(2025, 7, 19), _rows(day, [("04:00", "safe", "safe")]), flat).verdict == "조용"
    assert B.score_day(date(2025, 7, 19), _rows(day, [("04:00", "caution", None)]), None).verdict == ""


def test_has_data():
    assert not B.has_data({}) and not B.has_data({1: None})
    assert B.has_data({1: None, 2: 0})


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
