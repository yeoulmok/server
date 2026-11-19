"""사고 당일 재현(app/reconstruct.py) 테스트 — DB·HRFCO 없이 가짜로.  실행: python tests/test_reconstruct.py"""

import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import estimate as E  # noqa: E402
from app import reconstruct as R  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
STEP = timedelta(minutes=10)


def t(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def leaf(v):
    return {"leaf_value": v}


def model_24h(thr=20.0, low=0.01, high=0.10):
    """24시간 강수 > thr 이면 high m, 아니면 low m 를 추정하는 가짜 모델."""
    tree = {"split_feature": 4, "threshold": thr, "decision_type": "<=", "default_left": True,
            "missing_type": "None", "left_child": leaf(low), "right_child": leaf(high)}
    return E.parse_model({"model": {"objective": "regression", "feature_names": E.FEATURES,
                                    "tree_info": [{"tree_structure": tree}]}, "meta": {"check": []}})


def series_10m(start, end, rain_from, mm):
    """start~end 10분 자료. rain_from 부터 칸마다 mm, 그 전엔 0."""
    out, x = {}, start
    while x <= end:
        out[x] = D(str(mm)) if x >= rain_from else D("0")
        x += STEP
    return out


def test_no_future_data():
    # 12:00부터 칸당 3mm(=1시간 18mm). 11:50 판정엔 12:00 비가 안 섞여야 함
    s = series_10m(t("2025-07-16 08:00"), t("2025-07-19 16:00"), t("2025-07-19 12:00"), 3)
    v, rule = R.judge_at(s, R.J.KIND_RAIN_10M, t("2025-07-19 11:50"), R.DEFAULT_THRESHOLDS, None)
    assert v.rain.value == D("0") and v.level == "safe" and rule == "safe"
    v, rule = R.judge_at(s, R.J.KIND_RAIN_10M, t("2025-07-19 12:50"), R.DEFAULT_THRESHOLDS, None)
    assert v.rain.value == D("18") and v.level == "caution" and rule == "caution"


def test_ai_caution_before_rule():
    # 칸당 0.5mm(1시간 3mm)로 오래 → 1시간 규칙(15mm)은 안 걸리지만 24h ≥ 17mm + AI 0.10m → 주의(AI)
    s = series_10m(t("2025-07-16 00:00"), t("2025-07-19 16:00"), t("2025-07-19 00:00"), "0.5")
    rows = R.run(s, R.J.KIND_RAIN_10M, t("2025-07-19 02:00"), t("2025-07-19 16:00"),
                 R.DEFAULT_THRESHOLDS, model_24h(thr=15.0))
    ai = R.first_at(rows, "caution")
    assert R.first_at(rows, "caution", key=lambda r: r.rule_only) is None
    # 24h 강수: 05시 15.5mm, 06시 18.5mm(≥17) → 06:00 첫 주의
    assert ai == t("2025-07-19 06:00"), ai
    assert next(r for r in rows if r.at == ai).verdict.estimated
    lines = R.summary(rows, t("2025-07-19 14:09"))
    assert any("8시간 9분 전" in x for x in lines), lines
    assert any("규칙만으론 못 잡은" in x for x in lines), lines


def test_lead_text():
    acc = t("2025-07-19 14:09")
    assert R.lead(t("2025-07-19 11:00"), acc) == "사고 3시간 9분 전"
    assert R.lead(t("2025-07-19 14:30"), acc) == "사고 0시간 21분 뒤"
    assert R.lead(None, acc) == "안 뜸"


def test_hourly_fallback():
    calls = []

    def fake10(code, kind, s, e):
        calls.append("10m")
        return {}

    def fake1h(code, s, e):
        calls.append("1h")
        return R.parse_1h({"content": [{"ymdhm": "2025071913", "rf": "20"}, {"ymdhm": "2025071914", "rf": " "}]})

    kind, s = R.load_series("10064070", t("2025-07-19 00:00"), t("2025-07-19 16:00"), fake10, fake1h)
    assert calls == ["10m", "1h"] and kind == R.J.KIND_RAIN_1H
    assert s == {t("2025-07-19 13:00"): D("20"), t("2025-07-19 14:00"): None}
    v, rule = R.judge_at(s, kind, t("2025-07-19 13:00"), R.DEFAULT_THRESHOLDS, None)
    assert v.rain.value == D("20") and rule == "caution"


def test_csv(tmp=Path("/tmp/reconstruct_test.csv")):
    s = series_10m(t("2025-07-16 08:00"), t("2025-07-19 16:00"), t("2025-07-19 12:00"), 3)
    rows = R.run(s, R.J.KIND_RAIN_10M, t("2025-07-19 11:00"), t("2025-07-19 13:00"), R.DEFAULT_THRESHOLDS, None)
    R.save_csv(rows, tmp)
    lines = tmp.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 1 + 13 and lines[0].startswith("시각,")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
