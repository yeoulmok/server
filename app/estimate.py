"""강수만으로 하류 수위 상승폭을 추정하는 AI (LightGBM) — 판정 엔진용.

하류 수위관측소가 없는 계곡, 또는 수위 자료가 비었을 때 '주의' 단계를 확인하는 데 쓴다.
(4차 실험: 주의 = 24시간 강수 ≥ X mm 그리고 AI 추정 상승폭 ≥ 0.07m)

- 모델은 scripts/export_model.py 가 만든 data/model/rain_b.json (LightGBM dump_model JSON + meta).
- 트리 계산은 순수 파이썬 → judge 컨테이너에 lightgbm/libgomp 설치 불필요.
- 특징은 학습(train_v2.RAIN_COLS)과 같은 이름·순서:
  rain_1h, rain_3h, rain_6h, rain_12h, rain_24h, rain_72h, rain_max1h_6h, month
- 시간 강수: 기준 정시 H의 값 = H-1시 ~ H시 강수 (HRFCO 시자료와 같은 방식)
  · 10분 자료(rainfall): H-50분 ~ H시 6개 합 (하나라도 비면 그 시간은 빈 값)
  · 1시간 누적(rainfall_1h, 기상청 RN-60m): H시 정각 값
- 최근 6시간 중 빈 값이 있거나, 72시간 중 3시간 넘게 비면 추정 안 함 (사유 반환).
  그 이하로 빈 시간은 0mm로 본다. (학습 때는 빈 시간이 있는 행을 버렸음 — 한계)
"""

from __future__ import annotations

import json
import logging
import math
import os
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

log = logging.getLogger("estimate")

FEATURES = ["rain_1h", "rain_3h", "rain_6h", "rain_12h", "rain_24h", "rain_72h", "rain_max1h_6h", "month"]
HOURS = 72
RECENT_HOURS = 6                 # 이 안에 빈 시간이 있으면 추정 안 함
MAX_GAP_HOURS = 3                # 72시간 중 이보다 많이 비면 추정 안 함
ANCHOR_GRACE = timedelta(minutes=30)   # 이번 정시 값이 아직 안 들어왔으면 이 시간 안에서만 직전 정시 사용

RULE_24H_MM = Decimal("17")      # 4차 실험 0.1m 규칙 (모델 meta에 값이 있으면 그걸 씀)
AI_CAUTION_M = Decimal("0.07")   # 2단 알람 AI 기준 (0.7 × 0.1m)

MODEL_PATH = Path(os.environ.get("RAIN_MODEL_PATH",
                                 Path(__file__).resolve().parents[1] / "data" / "model" / "rain_b.json"))
CHECK_TOL = 1e-6

Series = dict[datetime, "Decimal | None"]
STEP = timedelta(minutes=10)
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Estimate:
    """한 강수 관측소 기준 추정 결과."""
    rain_24h: Decimal | None        # 규칙용 24시간 강수 (특징을 못 만들면 None)
    rise: Decimal | None            # AI 추정 상승폭 (m). 모델 없음·자료 부족이면 None
    observed_at: datetime | None    # 기준 정시
    reason: str | None = None       # rise가 None인 이유
    rule_mm: Decimal = RULE_24H_MM
    ai_m: Decimal = AI_CAUTION_M

    @property
    def rule_hit(self) -> bool:
        return self.rain_24h is not None and self.rain_24h >= self.rule_mm

    @property
    def ai_hit(self) -> bool:
        """2단 알람 '주의': 규칙 충족 + AI 추정 ≥ 기준. 추정을 못 했으면 False."""
        return self.rule_hit and self.rise is not None and self.rise >= self.ai_m


# ---------------------------------------------------------------------------
# 시간 강수 72개 → 특징
# ---------------------------------------------------------------------------

def _hour_floor(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def hourly_from_10min(series: Series, hour: datetime) -> Decimal | None:
    vals = [series.get(hour - STEP * i) for i in range(6)]
    return None if any(v is None for v in vals) else sum(vals, Decimal(0))


def hourly_from_1h(series: Series, hour: datetime) -> Decimal | None:
    return series.get(hour)


def hourly_rain(series: Series, now: datetime, per_hour) -> tuple[list[Decimal | None], datetime | None, str | None]:
    """([최근 시간부터 72개], 기준 정시, 실패 사유). per_hour = hourly_from_10min | hourly_from_1h."""
    anchor = _hour_floor(now)
    if per_hour(series, anchor) is None:
        if now - anchor > ANCHOR_GRACE:
            return [], None, "강수 자료 지연"
        anchor -= HOUR            # 정시 값이 아직 안 들어옴 → 직전 정시
    hours = [per_hour(series, anchor - HOUR * i) for i in range(HOURS)]
    if any(v is None for v in hours[:RECENT_HOURS]):
        return hours, anchor, "최근 6시간 강수 결측"
    if sum(v is None for v in hours) > MAX_GAP_HOURS:
        return hours, anchor, "72시간 강수 결측 많음"
    return hours, anchor, None


def rain_sum(series: Series, now: datetime, per_hour, hours: int = 24, max_gaps: int = 3) -> Decimal | None:
    """최근 `hours`시간 강수 합 (화면 표시용). 빈 시간이 max_gaps보다 많으면 None, 그 이하는 0으로."""
    anchor = _hour_floor(now)
    if per_hour(series, anchor) is None and now - anchor <= ANCHOR_GRACE:
        anchor -= HOUR
    vals = [per_hour(series, anchor - HOUR * i) for i in range(hours)]
    if sum(v is None for v in vals) > max_gaps:
        return None
    return sum((v for v in vals if v is not None), Decimal(0))


def features(hours: list[Decimal | None], anchor: datetime) -> dict[str, Decimal | int]:
    h = [v if v is not None else Decimal(0) for v in hours]
    f: dict[str, Decimal | int] = {f"rain_{w}h": sum(h[:w], Decimal(0)) for w in (1, 3, 6, 12, 24, 72)}
    f["rain_max1h_6h"] = max(h[:6])
    f["month"] = anchor.month
    return {k: f[k] for k in FEATURES}


# ---------------------------------------------------------------------------
# LightGBM JSON 트리 계산
# ---------------------------------------------------------------------------

K_ZERO = 1e-35   # LightGBM kZeroThreshold


def _f32(x: float) -> float:
    """학습 특징이 float32였으므로 같은 정밀도로 맞춘다."""
    return struct.unpack("f", struct.pack("f", float(x)))[0]


def _go_left(node: dict, x: float) -> bool:
    if node.get("decision_type", "<=") != "<=":
        raise ValueError(f"지원하지 않는 분기: {node.get('decision_type')}")
    mt = node.get("missing_type", "None")
    if math.isnan(x) and mt != "NaN":
        x = 0.0
    if (mt == "Zero" and abs(x) <= K_ZERO) or (mt == "NaN" and math.isnan(x)):
        return bool(node["default_left"])
    return x <= node["threshold"]


def _tree(node: dict, x: list[float]) -> float:
    while "leaf_value" not in node:
        node = node["left_child"] if _go_left(node, x[node["split_feature"]]) else node["right_child"]
    return node["leaf_value"]


@dataclass
class Model:
    trees: list[dict]
    feature_names: list[str]
    rule_mm: Decimal
    ai_m: Decimal
    meta: dict

    def predict(self, feats: dict) -> float:
        x = [_f32(feats[name]) for name in self.feature_names]
        return sum(_tree(t, x) for t in self.trees)


def parse_model(doc: dict) -> Model:
    m = doc["model"]
    meta = doc.get("meta", {})
    names = m["feature_names"]
    if names != FEATURES:
        raise ValueError(f"특징 목록이 다름: {names}")
    if m.get("objective", "regression").split()[0] not in ("regression", "regression_l2", "l2"):
        raise ValueError(f"회귀 모델이 아님: {m.get('objective')}")
    if m.get("average_output"):
        raise ValueError("average_output 모델(rf)은 지원 안 함")
    model = Model([t["tree_structure"] for t in m["tree_info"]], names,
                  Decimal(str(meta.get("rule_24h_mm", RULE_24H_MM))),
                  Decimal(str(meta.get("ai_caution_m", AI_CAUTION_M))), meta)
    # export 때 진짜 lightgbm으로 계산해 둔 예측값과 맞는지 확인
    worst = 0.0
    for c in meta.get("check", []):
        got = model.predict(dict(zip(names, c["x"])))
        worst = max(worst, abs(got - c["y"]))
    if worst > CHECK_TOL:
        raise ValueError(f"lightgbm 예측값과 다름 (최대 차이 {worst:.2e})")
    return model


_cache: dict = {"mtime": None, "model": None}


def load_model(path: Path = MODEL_PATH) -> Model | None:
    """파일이 없거나 깨졌으면 None (경고 로그). 파일이 바뀌면 다시 읽는다."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        if _cache["mtime"] != "missing":
            log.warning("AI 모델 파일 없음 (%s) → AI 확인 단계 건너뜀", path)
            _cache.update(mtime="missing", model=None)
        return None
    if mtime != _cache["mtime"]:
        try:
            _cache["model"] = parse_model(json.loads(path.read_text(encoding="utf-8")))
            log.info("AI 모델 읽음: %s (학습일 %s, 트리 %d개)", path,
                     _cache["model"].meta.get("trained_at"), len(_cache["model"].trees))
        except Exception:
            log.exception("AI 모델 읽기 실패 (%s) → AI 확인 단계 건너뜀", path)
            _cache["model"] = None
        _cache["mtime"] = mtime
    return _cache["model"]


# ---------------------------------------------------------------------------
# 판정 엔진용 진입점
# ---------------------------------------------------------------------------

def estimate(series: Series, now: datetime, per_hour, model: Model | None) -> Estimate:
    rule_mm = model.rule_mm if model else RULE_24H_MM
    ai_m = model.ai_m if model else AI_CAUTION_M
    hours, anchor, reason = hourly_rain(series, now, per_hour)
    if reason:
        return Estimate(None, None, anchor, reason, rule_mm, ai_m)
    f = features(hours, anchor)
    if model is None:
        return Estimate(f["rain_24h"], None, anchor, "AI 모델 없음", rule_mm, ai_m)
    rise = Decimal(str(round(model.predict(f), 4)))   # 비교는 이 값으로, DB에는 소수 2자리로 저장
    return Estimate(f["rain_24h"], rise, anchor, None, rule_mm, ai_m)


def merge(ests: list[Estimate]) -> Estimate | None:
    """관측소가 여럿이면 가장 위험한 쪽: 2단 충족 > 규칙 충족 > AI 추정값 > 24h 강수."""
    if not ests:
        return None
    return max(ests, key=lambda e: (e.ai_hit, e.rule_hit,
                                    e.rise is not None, e.rise or Decimal(0),
                                    e.rain_24h is not None, e.rain_24h or Decimal(0)))
