"""운영보고서(app/report.py) — 집계·검사만 (DB·API 없이).  실행: python tests/test_report.py"""

import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import report as R  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
S = datetime(2026, 11, 17, tzinfo=KST)
E = datetime(2026, 11, 24, tzinfo=KST)


def test_stage_minutes_counts_only_inside_period():
    rows = [(S - timedelta(days=1), "safe", False),                 # 기간 전부터 안전
            (S + timedelta(hours=10), "caution", True),             # AI로 주의 60분
            (S + timedelta(hours=11), "danger", False),             # 위험 30분
            (S + timedelta(hours=11, minutes=30), "unknown", False),  # 판정 불가 20분
            (S + timedelta(hours=11, minutes=50), "safe", False)]
    mins, top, changes, ai = R.stage_minutes(rows, S, E)
    assert (mins["caution"], mins["danger"], mins["unknown"]) == (60, 30, 20)
    assert mins["safe"] == 7 * 24 * 60 - 110 and top == "danger" and changes == 4 and ai == 1


def test_max_rain_and_rise():
    t0 = S
    ten = {t0 + R.STEP * i: D("3") for i in range(6)}                 # 1시간 18mm
    assert R.max_rain_1h(ten, "rainfall") == D("18")
    assert R.max_rain_1h({t0: D("12.5"), t0 + R.STEP: D("4")}, "rainfall_1h") == D("12.5")
    w = {t0: D("1.0"), t0 + timedelta(minutes=30): D("1.25"), t0 + timedelta(minutes=60): D("1.2")}
    assert R.max_rise_30m(w) == D("0.25")
    assert R.gap_pct({t0: D("1")}, t0, t0 + timedelta(hours=1)) == 83


def _stats():
    a = R.ValleyStats("금대리 계곡", "A 비+수위")
    a.minutes.update(caution=60, danger=30)
    a.top, a.alerts, a.rain_max_1h, a.rise_max_30m = "danger", 2, D("26.0"), D("0.25")
    a.gaps = {"원주 신림(AWS)": 12}
    b = R.ValleyStats("성남계곡", "B 비만")
    return [a, b]


def test_facts_and_check():
    f = R.facts_for(_stats(), S, E)
    assert any("금대리 계곡" in x and "위험 30분" in x and "신림" in x and "12%" in x for x in f.lines), f.lines
    ok = "## 요약\n- 7일 동안 경보 푸시 2번. 금대리 계곡이 위험까지 올랐고 최대 1시간 강수 26.0mm였어요.\n## 담당자 확인 필요\n- 원주 신림(AWS) 자료 빠짐 12%"
    assert R.check_draft(ok, f) is None, R.check_draft(ok, f)
    assert "사실에 없는 숫자" in R.check_draft("경보가 5번 있었어요", f)                # 지어낸 숫자
    assert R.check_draft("30분 뒤 도착할 수 있어요", f) == "도착 시간 예측"
    assert R.check_draft("가" * (R.MAX_CHARS + 1), f).startswith("너무 김")
    assert R.check_draft(ok, f, "max_tokens") == "중간에 잘림"


def test_table():
    t = R.table(_stats())
    assert "| 금대리 계곡 | A 비+수위 | 위험 | 90 | 0 | 2 | 0 | 26.0mm | 0.25m | 원주 신림(AWS) 12% |" in t


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
