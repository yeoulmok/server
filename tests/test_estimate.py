"""AI 추정(app/estimate.py) + 판정 엔진 AI 확인 단계 테스트.  실행: python tests/test_estimate.py

- 손으로 만든 작은 트리로 계산 규칙 확인 (늘 실행)
- data/model/rain_b.json 이 있으면: export 때 lightgbm이 낸 예측값과 비교 (VM에서)
- lightgbm이 설치돼 있으면: 작은 모델을 학습해 진짜 predict()와 비교 (VM 학습 컨테이너에서)
"""

import json
import random
import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import estimate as E  # noqa: E402
from app.judge import Signal, Threshold, decide  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 9, 14, 4, tzinfo=KST)
H14 = datetime(2026, 8, 9, 14, 0, tzinfo=KST)
TH = [Threshold("caution", D("15.0"), D("0.10")),
      Threshold("danger", D("25.0"), D("0.20")),
      Threshold("evacuate", D("40.0"), D("0.30"))]


def hourly_series(values, end=H14):
    """rainfall_1h 관측소: values[0]이 end 정시. 10분 정각마다 같은 값이 저장되는 것처럼 정시 값만."""
    return {end - timedelta(hours=i): v for i, v in enumerate(values)}


def tenmin_series(values, end=H14):
    """rainfall(10분) 관측소: values[0]이 end."""
    return {end - timedelta(minutes=10 * i): v for i, v in enumerate(values)}


# ---------------------------------------------------------------------------
# 시간 강수 · 특징
# ---------------------------------------------------------------------------

def test_features_from_1h():
    vals = [D(2), D(1), D(0), D(5), D(0), D(0)] + [D(1)] * 18 + [D(0)] * 48
    hours, anchor, reason = E.hourly_rain(hourly_series(vals), NOW, E.hourly_from_1h)
    assert reason is None and anchor == H14
    f = E.features(hours, anchor)
    assert list(f) == E.FEATURES
    assert f["rain_1h"] == D(2) and f["rain_3h"] == D(3) and f["rain_6h"] == D(8)
    assert f["rain_24h"] == D(26) and f["rain_72h"] == D(26)
    assert f["rain_max1h_6h"] == D(5) and f["month"] == 8


def test_rain_sum_24h():
    vals = [D(1)] * 22 + [None] * 2 + [D(5)] * 48
    assert E.rain_sum(hourly_series(vals), NOW, E.hourly_from_1h) == D(22)
    gappy = [D(1)] * 20 + [None] * 4 + [D(5)] * 48
    assert E.rain_sum(hourly_series(gappy), NOW, E.hourly_from_1h) is None


def test_features_from_10min_sums_six():
    s = tenmin_series([D("0.5")] * (73 * 6))
    hours, anchor, reason = E.hourly_rain(s, NOW, E.hourly_from_10min)
    assert reason is None and hours[0] == D(3)     # 0.5 × 6


def test_anchor_falls_back_when_hour_not_in_yet():
    vals = [None] + [D(1)] * 72                   # 14시 값이 아직 안 들어옴
    hours, anchor, reason = E.hourly_rain(hourly_series(vals), NOW, E.hourly_from_1h)
    assert reason is None and anchor == H14 - timedelta(hours=1)


def test_anchor_too_late_is_delay():
    vals = [None] + [D(1)] * 72
    _, _, reason = E.hourly_rain(hourly_series(vals), H14 + timedelta(minutes=40), E.hourly_from_1h)
    assert reason == "강수 자료 지연"


def test_recent_gap_refuses():
    vals = [D(1), D(1), None] + [D(1)] * 70
    _, _, reason = E.hourly_rain(hourly_series(vals), NOW, E.hourly_from_1h)
    assert reason == "최근 6시간 강수 결측"


def test_old_gaps_up_to_three_ok():
    vals = [D(1)] * 10 + [None] * 3 + [D(1)] * 59
    hours, anchor, reason = E.hourly_rain(hourly_series(vals), NOW, E.hourly_from_1h)
    assert reason is None and E.features(hours, anchor)["rain_72h"] == D(69)   # 빈 3시간은 0


def test_old_gaps_four_refuses():
    vals = [D(1)] * 10 + [None] * 4 + [D(1)] * 58
    _, _, reason = E.hourly_rain(hourly_series(vals), NOW, E.hourly_from_1h)
    assert reason == "72시간 강수 결측 많음"


# ---------------------------------------------------------------------------
# 트리 계산
# ---------------------------------------------------------------------------

def leaf(v):
    return {"leaf_value": v}


def split(feat, thr, left, right, missing="None", default_left=True):
    return {"split_feature": feat, "threshold": thr, "decision_type": "<=", "default_left": default_left,
            "missing_type": missing, "left_child": left, "right_child": right}


def model_doc(trees, check=None, meta=None):
    return {"model": {"objective": "regression", "feature_names": E.FEATURES,
                      "tree_info": [{"tree_structure": t} for t in trees]},
            "meta": {**(meta or {}), "check": check or []}}


F0 = dict(zip(E.FEATURES, [0, 0, 0, 0, 0, 0, 0, 8]))


def test_tree_sum_and_threshold_inclusive():
    m = E.parse_model(model_doc([split(4, 20.0, leaf(0.01), leaf(0.05)),       # rain_24h
                                 split(0, 5.0, leaf(0.0), leaf(0.04))]))         # rain_1h
    assert abs(m.predict({**F0, "rain_24h": 20}) - 0.01) < 1e-12               # <= 는 왼쪽
    assert abs(m.predict({**F0, "rain_24h": 21, "rain_1h": 6}) - 0.09) < 1e-12


def test_zero_missing_type_goes_default():
    # missing_type Zero: 0은 '결측'처럼 default 방향으로 (강수 0이 많아서 중요)
    m = E.parse_model(model_doc([split(0, 1.0, leaf(1.0), leaf(2.0), missing="Zero", default_left=False)]))
    assert m.predict({**F0, "rain_1h": 0}) == 2.0
    assert m.predict({**F0, "rain_1h": D("0.5")}) == 1.0


def test_float32_like_training():
    # 0.1은 float32로 0.10000000149… → 0.1(double) 기준보다 크다 → 오른쪽
    m = E.parse_model(model_doc([split(0, 0.1, leaf(1.0), leaf(2.0))]))
    assert m.predict({**F0, "rain_1h": D("0.1")}) == 2.0


def test_check_mismatch_rejected():
    doc = model_doc([leaf(0.5)], check=[{"x": [0] * 8, "y": 0.4}])
    try:
        E.parse_model(doc)
    except ValueError:
        return
    raise AssertionError("예측값이 다른데 통과함")


def test_wrong_features_rejected():
    doc = model_doc([leaf(0.5)])
    doc["model"]["feature_names"] = list(reversed(E.FEATURES))
    try:
        E.parse_model(doc)
    except ValueError:
        return
    raise AssertionError("특징 순서가 다른데 통과함")


def test_meta_overrides_rule():
    m = E.parse_model(model_doc([leaf(0.5)], meta={"rule_24h_mm": 18.0, "ai_caution_m": 0.08}))
    assert m.rule_mm == D("18.0") and m.ai_m == D("0.08")


def test_missing_file_is_none(tmp=Path("/nonexistent/rain_b.json")):
    assert E.load_model(tmp) is None


def test_estimate_end_to_end():
    m = E.parse_model(model_doc([split(4, 17.0, leaf(0.02), leaf(0.09))]))
    wet = hourly_series([D(3)] * 6 + [D(0)] * 66)          # 24h = 18mm
    e = E.estimate(wet, NOW, E.hourly_from_1h, m)
    assert e.rain_24h == D(18) and e.rise == D("0.09") and e.rule_hit and e.ai_hit
    e2 = E.estimate(wet, NOW, E.hourly_from_1h, None)
    assert e2.rise is None and e2.reason == "AI 모델 없음" and e2.rain_24h == D(18) and not e2.ai_hit


# ---------------------------------------------------------------------------
# 판정 엔진 AI 확인 단계
# ---------------------------------------------------------------------------

T = H14
RAIN0 = Signal(D(2), T)                        # 1시간 강수 2mm (주의 기준 15mm 아래)
NOWATER = Signal(None, None, "연결된 관측소 없음")
WATER_MISSING = Signal(None, T, "수위 결측")


def est(r24, rise, reason=None):
    return E.Estimate(D(r24) if r24 is not None else None,
                      D(rise) if rise is not None else None, T, reason)


def test_no_water_two_stage_caution():
    v = decide(RAIN0, NOWATER, TH, est(18, "0.08"), has_water=False)
    assert v.level == "caution" and v.estimated


def test_no_water_rule_but_ai_says_low_is_safe():
    v = decide(RAIN0, NOWATER, TH, est(18, "0.05"), has_water=False)
    assert v.level == "safe" and not v.estimated              # 헛경보 줄이는 부분


def test_no_water_not_unknown_anymore():
    v = decide(RAIN0, NOWATER, TH, est(3, "0.01"), has_water=False)
    assert v.level == "safe"                                  # 수위 없음 ≠ 결측


def test_no_water_no_model_uses_rule_only():
    v = decide(RAIN0, NOWATER, TH, est(18, None, "AI 모델 없음"), has_water=False)
    assert v.level == "caution" and v.estimated


def test_no_water_24h_unbuildable_is_unknown():
    v = decide(RAIN0, NOWATER, TH, est(None, None, "최근 6시간 강수 결측"), has_water=False)
    assert v.level == "unknown"


def test_no_water_hard_rules_unchanged():
    v = decide(Signal(D(41), T), NOWATER, TH, est(50, "0.01"), has_water=False)
    assert v.level == "evacuate" and not v.estimated          # AI가 낮게 봐도 대피 그대로


def test_water_missing_ai_raises_unknown_to_caution():
    v = decide(RAIN0, WATER_MISSING, TH, est(18, "0.08"))
    assert v.level == "caution" and v.estimated


def test_water_missing_ai_low_stays_unknown():
    v = decide(RAIN0, WATER_MISSING, TH, est(18, "0.01"))
    assert v.level == "unknown"                               # AI는 올리기만


def test_water_missing_no_model_stays_unknown():
    v = decide(RAIN0, WATER_MISSING, TH, est(18, None, "AI 모델 없음"))
    assert v.level == "unknown"                               # 수위 있는 계곡은 지금처럼


def test_water_present_ignores_ai():
    v = decide(RAIN0, Signal(D("0.02"), T), TH, est(18, "0.09"))
    assert v.level == "safe" and v.est is None and not v.estimated


def test_estimated_false_when_rule_already_caution():
    v = decide(Signal(D(16), T), WATER_MISSING, TH, est(18, "0.08"))
    assert v.level == "caution" and not v.estimated           # 1시간 강수만으로도 주의


# ---------------------------------------------------------------------------
# 진짜 lightgbm과 비교 (있을 때만)
# ---------------------------------------------------------------------------

def test_model_file_matches_lightgbm_if_present():
    if not E.MODEL_PATH.exists():
        print("  (건너뜀: 모델 파일 없음)", end="")
        return
    doc = json.loads(E.MODEL_PATH.read_text(encoding="utf-8"))
    assert len(doc["meta"]["check"]) >= 100
    E.parse_model(doc)              # 차이가 1e-6 넘으면 ValueError


def test_against_real_lightgbm_if_installed():
    try:
        import lightgbm as lgb
        import numpy as np
        import pandas as pd
    except ImportError:
        print("  (건너뜀: lightgbm 없음)", end="")
        return
    rng = random.Random(0)
    rows = []
    for _ in range(3000):
        h = [rng.choice([0.0, 0.0, 0.0, 0.5, 1.0, 3.0, 8.0, 20.0]) for _ in range(72)]
        rows.append([h[0], sum(h[:3]), sum(h[:6]), sum(h[:12]), sum(h[:24]), sum(h), max(h[:6]),
                     rng.choice([6, 7, 8, 9])])
    X = pd.DataFrame(rows, columns=E.FEATURES).astype("float32").astype({"month": "int32"})
    y = 0.004 * X["rain_24h"] + 0.01 * X["rain_max1h_6h"] + np.random.default_rng(0).normal(0, 0.02, len(X))
    gb = lgb.LGBMRegressor(n_estimators=200, num_leaves=31, min_child_samples=20, verbose=-1).fit(X, y)
    doc = {"model": gb.booster_.dump_model(), "meta": {}}
    m = E.parse_model(json.loads(json.dumps(doc)))
    want = gb.predict(X)
    got = [m.predict(dict(zip(E.FEATURES, r))) for r in X.itertuples(index=False)]
    worst = max(abs(a - b) for a, b in zip(got, want))
    assert worst < 1e-6, worst


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
