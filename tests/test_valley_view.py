"""앱 응답 모양 단위 테스트 (DB·FastAPI 불필요).  실행: python tests/test_valley_view.py"""

import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import valley_view as vv  # noqa: E402
from app.judge import Signal  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 9, 17, 0, tzinfo=KST)
VALLEY = (1, "금대리 계곡", "원주시 판부면", D("37.278313"), D("128.019563"))
TH = [("caution", D("15.0"), D("0.10")), ("danger", D("25.0"), D("0.20")), ("evacuate", D("40.0"), D("0.30"))]


def test_fresh_status():
    row = vv.summary_row(VALLEY, ("safe", D("0.0"), D("0.00"), NOW - timedelta(minutes=10)), NOW, True)
    assert row["level"] == "safe" and row["stale"] is False
    assert row["rain_1h_mm"] == 0.0 and row["observed_at"] == "2026-11-09T16:50"
    assert row["lat"] == 37.278313 and row["subscribed"] is True


def test_stale_after_15min():
    row = vv.summary_row(VALLEY, ("safe", D(0), D(0), NOW - timedelta(minutes=20)), NOW, False)
    assert row["level"] == "safe" and row["stale"] is True


def test_unknown_after_30min():
    row = vv.summary_row(VALLEY, ("safe", D(0), D(0), NOW - timedelta(minutes=31)), NOW, False)
    assert row["level"] == "unknown" and row["stale"] is True


def test_no_status_is_unknown_and_null():
    row = vv.summary_row(VALLEY, None, NOW, False)
    assert row["level"] == "unknown" and row["rain_1h_mm"] is None and row["observed_at"] is None


def test_threshold_pick():
    lim = {"caution": D(15), "danger": D(25), "evacuate": D(40)}
    assert vv.pick_threshold(D(18), lim) == 15.0
    assert vv.pick_threshold(D(26), lim) == 25.0
    assert vv.pick_threshold(D(0), lim) == 15.0
    assert vv.pick_threshold(None, lim) == 15.0


def test_observation_missing_keeps_null():
    o = vv.observation("rain_1h", "원주시(신촌리)", Signal(None, None, "강수 결측"), TH)
    assert o["value"] is None and o["note"] == "강수 결측" and o["unit"] == "mm" and o["threshold"] == 15.0


def test_source_order():
    assert vv.source_text(["kma", "hrfco"]) == "자료: 한강홍수통제소, 기상청"


def test_detail_fallback_text():
    d = vv.detail(VALLEY, ("caution", D(18), D("0.06"), NOW - timedelta(minutes=5), None, False),
                  [], NOW, False, ["hrfco"])
    assert d["level"] == "caution" and d["summary"] == "상류에 비가 많이 오고 있어요"
    assert d["ai_explanation"] is None and d["ai_fallback"] is True
    assert d["disclaimer"].startswith("판단 보조")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
