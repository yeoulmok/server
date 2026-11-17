"""판정 엔진용 AI 모델 내보내기 (4차 실험 train_v4.py와 같은 학습).

LightGBM(강수만, train_v2.RAIN_COLS)을 학습해서
  data/model/rain_b.json = {"model": booster.dump_model(), "meta": {...}}
으로 저장한다. judge는 app/estimate.py가 이 JSON 트리를 순수 파이썬으로 계산한다.

meta
  features      특징 이름·순서 (estimate.FEATURES와 같아야 함)
  rule_24h_mm   2단 알람 1단계 규칙 X (best_rule, 0.1m) — 4차 결과 17mm 근처
  ai_caution_m  2단 알람 AI 기준 0.07m (= 0.7 × 0.1m)
  check         시험 기간 200행의 (특징, lightgbm 예측값). judge가 읽을 때 자기 계산과 비교해서
                1e-6 넘게 다르면 모델을 안 쓴다.

data/는 깃 제외 → 모델 파일은 VM에만 있음. 다시 학습하면 judge가 파일 변경을 보고 새로 읽는다.

실행 (VM):
  docker run --rm --name export_model --memory 2g --env-file ~/secrets/yeoulmok.env -v ~/yeoulmok:/app -w /app python:3.12-slim sh -c \
    "apt-get update -qq && apt-get install -y -qq libgomp1 >/dev/null && pip install -q numpy pandas scikit-learn lightgbm httpx tzdata && python -u scripts/export_model.py && python tests/test_estimate.py"
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import train_pipeline as v1  # noqa: E402
import train_v2 as v2  # noqa: E402
import train_v3 as v3  # noqa: E402
from app import estimate as E  # noqa: E402

THR = 0.1        # 주의 단계 상승폭
K = 0.7          # 4차 결과: 2단(AI ≥ 0.07m)
OUT = Path(__file__).resolve().parents[1] / "data" / "model" / "rain_b.json"
N_CHECK = 200
log = v1.log


def main():
    import lightgbm as lgb

    assert v2.RAIN_COLS == E.FEATURES, (v2.RAIN_COLS, E.FEATURES)
    pairs = json.load(open(f"{v1.ROOT}/pairs.json"))
    tables = []
    for pr in pairs:
        if pr["wl"] == v2.HOLD_WL:
            continue
        df = v1.station_frame(pr)
        if df is None:
            continue
        keep, _ = v2.keep_station(v1.metrics(df))
        if keep:
            tables.append(v2.table(df, pr["wl"]))
    data = pd.concat(tables)
    tr = data[data["year"].isin(v2.TRAIN_Y)]
    va = data[data["year"].isin(v2.VALID_Y)]
    te = data[data["year"].isin(v2.TEST_Y)]
    log(f"관측소 {len(tables)}곳, 학습 {len(tr)} / 검증 {len(va)} / 시험 {len(te)}")

    B = v2.RAIN_COLS
    gb = lgb.LGBMRegressor(n_estimators=3000, learning_rate=0.03, num_leaves=31, min_child_samples=100,
                           subsample=0.8, subsample_freq=1, colsample_bytree=0.9, n_jobs=2, verbose=-1)
    gb.fit(tr[B], tr["target"], eval_set=[(va[B], va["target"])], callbacks=[lgb.early_stopping(100, verbose=False)])
    best = gb.best_iteration_
    rule = v3.best_rule(tr["rain_24h"].values, tr["target"].values, THR)
    log(f"best_iteration {best}, 규칙24h X = {rule:.2f}mm")

    sample = te[B].sample(n=min(N_CHECK, len(te)), random_state=0)
    pred = gb.predict(sample)
    check = [{"x": [float(v) for v in row], "y": float(y)} for row, y in zip(sample.itertuples(index=False), pred)]

    doc = {
        "model": gb.booster_.dump_model(num_iteration=best),
        "meta": {
            "features": B,
            "rule_24h_mm": round(float(rule), 2),
            "ai_caution_m": round(K * THR, 4),
            "threshold_m": THR,
            "trained_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="minutes"),
            "best_iteration": best,
            "n_train": len(tr), "n_valid": len(va), "n_test": len(te), "stations": len(tables),
            "source": "scripts/export_model.py (train_v4와 같은 학습)",
            "check": check,
        },
    }
    # 자기 확인: 순수 파이썬 계산이 lightgbm과 같은지 (다르면 저장 안 함)
    E.parse_model(json.loads(json.dumps(doc)))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)       # judge가 쓰다 만 파일을 읽지 않게
    log(f"저장: {OUT} (트리 {len(doc['model']['tree_info'])}개, {OUT.stat().st_size // 1024}KB)")


if __name__ == "__main__":
    main()
