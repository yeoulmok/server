"""학습 실험 한 번에 돌리기: 관측소 거르기 → 학습 표 → 선형회귀 vs LightGBM(+SHAP).

입력 : data/train/pairs.json, data/train/raw/*.json  (scripts/fetch_train.py 결과)
출력 : data/train/out/
  - stations.csv     관측소별 지표와 사용 여부(keep), 제외 이유
  - results.json     모델별 성능 (연도 분리 검증 / 시험, 동부교 따로)
  - shap.csv         LightGBM 특징 중요도 (평균 |SHAP|)
  - lgb_model.txt    LightGBM 모델
실행 (VM, 네트워크·키 필요 없음):
  docker run -d --name train --memory 2g -v ~/yeoulmok:/app -w /app python:3.12-slim sh -c \
    "apt-get update -qq && apt-get install -y -qq libgomp1 >/dev/null && pip install -q numpy pandas scikit-learn lightgbm && python -u scripts/train_pipeline.py >> data/train/train.log 2>&1"

정답(타깃) : 1시간 뒤 수위 - 지금 수위 (m)
분리       : 연도 단위 (학습 2010~2019 / 검증 2020~2022 / 시험 2023~2025) — 같은 비가 학습·시험에 섞이지 않게
동부교     : 1006664는 학습에서 빼고 따로 시험 (최종 시험 대상이라 학습에 넣지 않음)
"""
import glob
import json
import os
import time

import numpy as np
import pandas as pd

ROOT = os.environ.get("TRAIN_DIR", "/app/data/train")
RAW = f"{ROOT}/raw"
OUT = f"{ROOT}/out"
HOLDOUT = {"1006664"}                     # 금대리 하류 동부교 — 학습 제외
TRAIN_Y, VALID_Y, TEST_Y = range(2010, 2020), range(2020, 2023), range(2023, 2026)
RAIN_WINDOWS = (1, 3, 6, 12, 24)
JUMP_LIMIT = 2.0                          # 1시간에 2m 넘게 변하면 센서 이상으로 보고 버림

# 관측소 거르기 기준 (stations.csv 보고 조정)
MIN_HOURS = 5000                          # 수위·강수가 둘 다 있는 시간 수
MIN_CORR = 0.25                           # 6시간 강수 ↔ 그 뒤 수위 상승폭 상관 (스피어만)
MIN_EVENT_RISE = 0.05                     # 큰 비(상위 1%) 뒤 최대 상승폭 중앙값 (m)
MAX_LAG_H = 12                            # 강수 → 수위 반응 지연(시간) — 길면 큰 강 본류로 봄
os.makedirs(OUT, exist_ok=True)


def log(*a):
    print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)


def to_time(s):
    """HRFCO 1시간 자료 시각 'YYYYMMDDHH' (24시 표기도 처리)."""
    s = str(s).strip()
    if len(s) < 10:
        return None
    base = pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:8]}")
    return base + pd.Timedelta(hours=int(s[8:10]))


def load(kind, code):
    vals = {}
    for path in sorted(glob.glob(f"{RAW}/{kind}_{code}_*.json")):
        try:
            rows = json.load(open(path))
        except Exception:
            continue
        for t, v in rows:
            ts = to_time(t)
            if ts is not None and v is not None:
                vals[ts] = v
    if not vals:
        return pd.Series(dtype="float32")
    return pd.Series(vals, dtype="float32").sort_index()


def station_frame(pr):
    wl, rf = load("waterlevel", pr["wl"]), load("rainfall", pr["rf"])
    if wl.empty or rf.empty:
        return None
    idx = pd.date_range(min(wl.index.min(), rf.index.min()), max(wl.index.max(), rf.index.max()), freq="h")
    df = pd.DataFrame({"wl": wl.reindex(idx), "rain": rf.reindex(idx)})
    df = df[df.index.month.isin([6, 7, 8, 9])]
    df.loc[df["rain"] < 0, "rain"] = np.nan
    jump = df["wl"].diff().abs() > JUMP_LIMIT
    df.loc[jump, "wl"] = np.nan
    return df


def metrics(df):
    both = df.dropna()
    m = {"hours": int(len(both))}
    if len(both) < 500:
        return m
    r6 = df["rain"].rolling(6, min_periods=6).sum()
    # 비 오기 직전(6시간 전) 수위 대비, 그 뒤 18시간 안의 최고 수위 상승폭
    rise6 = df["wl"].shift(-12).rolling(18, min_periods=6).max() - df["wl"].shift(6)
    ok = r6.notna() & rise6.notna() & (r6 > 0)
    m["corr"] = float(r6[ok].corr(rise6[ok], method="spearman")) if ok.sum() > 200 else None
    big = r6[ok] >= np.nanpercentile(r6[ok], 99) if ok.sum() > 200 else None
    m["event_rise"] = float(np.nanmedian(rise6[ok][big])) if big is not None and big.sum() > 5 else None
    # 반응 지연: 강수와 이후 수위 변화의 교차상관이 가장 큰 시차
    dwl = df["wl"].diff()
    best, lag = -1.0, None
    for k in range(0, 25):
        c = df["rain"].corr(dwl.shift(-k))
        if c is not None and not np.isnan(c) and c > best:
            best, lag = c, k
    m["lag_h"], m["lag_corr"] = lag, (float(best) if lag is not None else None)
    m["wl_range"] = float(np.nanpercentile(df["wl"], 99) - np.nanpercentile(df["wl"], 1))
    return m


def judge_station(m):
    if m.get("hours", 0) < MIN_HOURS:
        return False, f"자료 부족 ({m.get('hours', 0)}시간)"
    if m.get("corr") is None or m["corr"] < MIN_CORR:
        return False, f"강수-수위 상관 낮음 ({m.get('corr')})"
    if m.get("event_rise") is None or m["event_rise"] < MIN_EVENT_RISE:
        return False, f"큰 비에도 수위 반응 작음 ({m.get('event_rise')})"
    if m.get("lag_h") is not None and m["lag_h"] > MAX_LAG_H:
        return False, f"반응 지연 김 ({m['lag_h']}시간) — 큰 강·댐 영향 의심"
    return True, ""


def features(df, code):
    f = pd.DataFrame(index=df.index)
    for w in RAIN_WINDOWS:
        f[f"rain_{w}h"] = df["rain"].rolling(w, min_periods=w).sum()
    f["rain_max1h_6h"] = df["rain"].rolling(6, min_periods=6).max()
    base = df["wl"].rolling(24 * 7, min_periods=24).median()       # 최근 1주 평소 수위
    f["wl_above_base"] = df["wl"] - base
    f["wl_change_1h"] = df["wl"].diff(1)
    f["wl_change_3h"] = df["wl"].diff(3)
    f["month"] = df.index.month
    f["target"] = df["wl"].shift(-1) - df["wl"]
    f["year"] = df.index.year
    f["station"] = code
    f = f.dropna()
    # 비가 온 시간대만 (24시간 강수 1mm 이상) — 맑은 날 0이 대부분을 차지하지 않게
    return f[f["rain_24h"] >= 1].astype({c: "float32" for c in f.columns if c not in ("station", "year", "month")})


def scores(y, p):
    y, p = np.asarray(y), np.asarray(p)
    out = {"n": int(len(y)), "mae": float(np.mean(np.abs(y - p))), "rmse": float(np.sqrt(np.mean((y - p) ** 2)))}
    big = y >= 0.05
    out["n_big"] = int(big.sum())
    if big.any():
        out["mae_big"] = float(np.mean(np.abs(y[big] - p[big])))
    for thr in (0.05, 0.10):
        t, h = y >= thr, p >= thr
        tp = int((t & h).sum())
        out[f"recall_{thr}"] = float(tp / t.sum()) if t.sum() else None
        out[f"precision_{thr}"] = float(tp / h.sum()) if h.sum() else None
    return out


def main():
    pairs = json.load(open(f"{ROOT}/pairs.json"))
    log("짝 목록", len(pairs), "곳")
    rows, tables = [], []
    for i, pr in enumerate(pairs, 1):
        df = station_frame(pr)
        if df is None:
            rows.append({**pr, "keep": False, "reason": "자료 없음"})
            continue
        m = metrics(df)
        keep, reason = judge_station(m)
        if pr["wl"] in HOLDOUT:
            keep, reason = False, "동부교 — 최종 시험용 (학습 제외)"
        rows.append({**pr, **m, "keep": keep, "reason": reason})
        if keep or pr["wl"] in HOLDOUT:
            t = features(df, pr["wl"])
            tables.append(t)
        log(f"[{i}/{len(pairs)}] {pr['wl']} {pr['wl_name']} keep={keep} {reason} {m}")
    pd.DataFrame(rows).to_csv(f"{OUT}/stations.csv", index=False, encoding="utf-8-sig")
    kept = sum(r["keep"] for r in rows)
    log(f"관측소 거르기 끝: {kept}/{len(pairs)}곳 사용 → {OUT}/stations.csv")

    data = pd.concat(tables)
    hold = data[data["station"].isin(HOLDOUT)]
    data = data[~data["station"].isin(HOLDOUT)]
    feats = [c for c in data.columns if c not in ("target", "year", "station")]
    split = {"train": data[data["year"].isin(TRAIN_Y)], "valid": data[data["year"].isin(VALID_Y)],
             "test": data[data["year"].isin(TEST_Y)]}
    log("학습 표:", {k: len(v) for k, v in split.items()}, "동부교:", len(hold), "특징:", feats)

    from sklearn.linear_model import LinearRegression
    import lightgbm as lgb

    res = {"features": feats, "rows": {k: len(v) for k, v in split.items()}, "holdout_rows": len(hold),
           "stations_used": kept, "split": {"train": "2010-2019", "valid": "2020-2022", "test": "2023-2025"}}
    res["zero"] = {k: scores(v["target"], np.zeros(len(v))) for k, v in split.items() if k != "train"}

    lr = LinearRegression().fit(split["train"][feats], split["train"]["target"])
    res["linear"] = {k: scores(v["target"], lr.predict(v[feats])) for k, v in split.items() if k != "train"}
    if len(hold):
        res["linear"]["holdout_동부교"] = scores(hold["target"], lr.predict(hold[feats]))
    res["linear"]["coef"] = dict(zip(feats, map(float, lr.coef_)))
    log("선형회귀:", json.dumps(res["linear"]["test"], ensure_ascii=False))

    model = lgb.LGBMRegressor(n_estimators=2000, learning_rate=0.03, num_leaves=31, min_child_samples=50,
                              subsample=0.8, subsample_freq=1, colsample_bytree=0.9, n_jobs=2, verbose=-1)
    model.fit(split["train"][feats], split["train"]["target"],
              eval_set=[(split["valid"][feats], split["valid"]["target"])],  # noqa (lightgbm 4.x)
              callbacks=[lgb.early_stopping(100, verbose=False)])
    res["lightgbm"] = {k: scores(v["target"], model.predict(v[feats])) for k, v in split.items() if k != "train"}
    if len(hold):
        res["lightgbm"]["holdout_동부교"] = scores(hold["target"], model.predict(hold[feats]))
    res["lightgbm"]["best_iteration"] = int(model.best_iteration_ or 0)
    log("LightGBM:", json.dumps(res["lightgbm"]["test"], ensure_ascii=False))
    model.booster_.save_model(f"{OUT}/lgb_model.txt")

    sample = split["test"].sample(min(20000, len(split["test"])), random_state=0)
    contrib = model.predict(sample[feats], pred_contrib=True)[:, :-1]          # 마지막 열은 기준값
    shap = pd.Series(np.abs(contrib).mean(axis=0), index=feats).sort_values(ascending=False)
    shap.rename("mean_abs_shap").to_csv(f"{OUT}/shap.csv", encoding="utf-8-sig")
    res["shap_top"] = {k: float(v) for k, v in shap.head(8).items()}

    json.dump(res, open(f"{OUT}/results.json", "w"), ensure_ascii=False, indent=1)
    log("끝. 결과 →", f"{OUT}/results.json")


if __name__ == "__main__":
    main()
