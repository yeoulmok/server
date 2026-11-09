"""판정 로직 단위 테스트 (DB 불필요).  실행: python tests/test_judge.py"""

import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.judge import (Signal, Threshold, decide, rain_1h, water_rise_30m)  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
T = datetime(2026, 11, 9, 14, 50, tzinfo=KST)
NOW = T + timedelta(minutes=4)
TH = [Threshold("caution", D("15.0"), D("0.10")),
      Threshold("danger", D("25.0"), D("0.20")),
      Threshold("evacuate", D("40.0"), D("0.30"))]


def series(values, end=T):
    """values[0]이 가장 최근. 10분 간격."""
    return {end - timedelta(minutes=10 * i): v for i, v in enumerate(values)}


def test_rain_sum():
    s = rain_1h(series([D(3)] * 6 + [D(100)]), NOW)   # 7번째(1시간 전)는 합계에서 빠짐
    assert s.value == D(18)


def test_rain_blank_is_unknown():
    s = rain_1h(series([None] * 7), NOW)
    assert s.value is None and s.reason == "강수 결측"


def test_rain_stale():
    s = rain_1h(series([D(0)] * 7), T + timedelta(minutes=41))
    assert s.value is None and s.reason == "강수 자료 지연"


def test_rise():
    s = water_rise_30m(series([D("0.35"), D("0.30"), D("0.20"), D("0.15")]), NOW)
    assert s.value == D("0.20")


def test_rise_missing_past():
    s = water_rise_30m(series([D("0.35"), D("0.30"), D("0.20"), None]), NOW)
    assert s.value is None


def test_normal():
    assert decide(Signal(D(0), T), Signal(D(0), T), TH).level == "normal"


def test_highest_wins():
    assert decide(Signal(D(16), T), Signal(D("0.31"), T), TH).level == "evacuate"


def test_boundary_inclusive():
    assert decide(Signal(D(25), T), Signal(D(0), T), TH).level == "danger"


def test_unknown_when_missing_and_calm():
    v = decide(Signal(None, None, "강수 결측"), Signal(D(0), T), TH)
    assert v.level == "unknown"


def test_alert_even_if_other_missing():
    v = decide(Signal(None, None, "강수 결측"), Signal(D("0.22"), T), TH)
    assert v.level == "danger"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
