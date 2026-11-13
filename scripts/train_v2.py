"""학습 실험 2차: 이상 관측소 정리 + '강수만' 모델 + 동부교 2026 최종 시험.

1차(train_pipeline.py)에서 나온 문제
  - 모델이 '최근 수위 변화'에 기댐 → 하류 수위가 없는 계곡(횡성·서곡리)에선 못 씀
  - 수위 범위가 수십~수백 m인 이상 관측소, 큰 강 본류가 섞임
  - 동부교가 69곳에 없어서 최종 시험을 못 함

이번에 하는 것
  - 관측소: 수위 범위 3m 초과(이상값·큰 강) / 반응 지연 3시간 초과 / 상관 0.4 미만 제외
  - 정답: 앞으로 3시간 최고 수위 - 평소 수위(최근 1주 중앙값)  = "평소보다 얼마나 차오르나"
  - 모델 A (수위 있음): 강수 + 수위 특징   / 모델 B (강수만): 강수 특징만  ← 하류 수위 없는 계곡용
  - 각각 선형회귀 vs LightGBM, 연도 분리 (학습 2010~2019 / 검증 2020~2022 / 시험 2023~2025)
  - 동부교(1006664) 2026년 5~10월 자료를 받아 최종 시험 (학습에는 안 씀)

출력: data/train/out_v2/ (stations.csv, results.json, shap_B.csv, holdout_events.csv)
실행 (HRFCO 키 필요 → --env-file):
  docker run -d --name train2 --memory 2g --env-file ~/secrets/yeoulmok.env -v ~/yeoulmok:/app -w /app python:3.12-slim sh -c \
    "apt-get update -qq && apt-get install -y -qq libgomp1 >/dev/null && pip install -q numpy pandas scikit-learn lightgbm httpx && python -u scripts/train_v2.py >> data/train/train_v2.log 2>&1"
"""
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import train_pipeline as v1  # noqa: E402  (load, station_frame, metrics, to_time 재사용)

ROOT = v1.ROOT
OUT = f"{ROOT}/out_v2"
HOLD_WL = "1006664"                        # 원주시(동부교)
HOLD_YEAR, HOLD_MONTHS = 2026, (5, 6, 7, 8, 9, 10)
TRAIN_Y, VALID_Y, TEST_Y = range(2010, 2020), range(2020, 2023), range(2023, 2026)
MAX_WL_RANGE = 3.0                         # 수위 1~99% 범위 (m). 넘으면 큰 강이거나 이상값
MAX_LAG_H = 3                              # 강수 → 수위 반응 지연 (시간)
MIN_CORR = 0.4
MIN_HOURS = 5000
HORIZON = 3                                # 앞으로 몇 시간 안의 최고 수위
THRESHOLDS = (0.1, 0.2, 0.3)               # 판정 기준(주의/위험/대피 상승폭)과 같은 눈금
RAIN_COLS = ["rain_1h", "rain_3h", "rain_6h", "rain_12h", "rain_24h", "rain_72h", "rain_max1h_6h", "month"]
WL_COLS = ["wl_above_base", "wl_change_1h", "wl_change_3h"]
os.makedirs(OUT, exist_ok=True)
log = v1.log


def keep_station(m):
    if m.get("hours", 0) < MIN_HOURS:
        return False, f"자료 부족 ({m.get('hours', 0)}시간)"
    if m.get("wl_range") is None or m["wl_range"] > MAX_WL_RANGE:
        return False, f"수위 범위 {m.get('wl_range', 0):.1f}m — 큰 강·이상값"
    if m.get("corr") is None or m["corr"] < MIN_CORR:
        return False, f"강수-수위 상관 낮음 ({m.get('corr')})"
    if m.get("lag_h") is None or m["lag_h"] > MAX_LAG_H:
        return False, f"반응 지연 {m.get('lag_h')}시간 — 큰 강 의심"
    return True, ""


def table(df, code):
    f = pd.DataFrame(index=df.index)
    for w in (1, 3, 6, 12, 24, 72):
        f[f"rain_{w}h"] = df["rain"].rolling(w, min_periods=w).sum()
    f["rain_max1h_6h"] = df["rain"].rolling(6, min_periods=6).max()
    base = df["wl"].rolling(24 * 7, min_periods=24).median()
    f["wl_above_base"] = df["wl"] - base
    f["wl_change_1h"] = df["wl"].diff(1)
    f["wl_change_3h"] = df["wl"].diff(3)
    f["month"] = df.index.month
    future_max = df["wl"].shift(-HORIZON).rolling(HORIZON, min_periods=HORIZON).max()
    f["target"] = future_max - base
    f["year"] = df.index.year
    f["station"] = code
    f = f.dropna()
    f = f[f["rain_24h"] >= 1]
    num = [c for c in f.columns if c not in ("station", "year", "month")]
    return f.astype({c: "float32" for c in num})


def scores(y, p):
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    out = {"n": int(len(y)), "mae": round(float(np.mean(np.abs(y - p))), 4)}
    for thr in THRESHOLDS:
        t, h = y >= thr, p >= thr
        tp = int((t & h).sum())
        out[f"{thr}m"] = {"실제": int(t.sum()), "잡음(recall)": round(tp / t.sum(), 3) if t.sum() else None,
                         "맞춤(precision)": round(tp / h.sum(), 3) if h.sum() else None}
    return out


# ---------------------------------------------------------------- 동부교 2026 받기

def dms(s):
    try:
        d, m, sec = [float(p) for p in str(s).strip().split("-")]
        return d + m / 60 + sec / 3600
    except Exception:
        return None


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    return 6371 * math.hypot((lo2 - lo1) * math.cos((la1 + la2) / 2), la2 - la1)


def fetch_holdout():
    """동부교 수위 + 가까운 우량관측소(15km 안, 2026년 자료가 가장 많은 곳) 1시간 자료."""
    import calendar
    import httpx
    base = "https://api.hrfco.go.kr/" + os.environ["HRFCO_KEY"]
    raw = f"{ROOT}/holdout"
    os.makedirs(raw, exist_ok=True)

    def month(cl, kind, code, m):
        path = f"{raw}/{kind}_{code}_{HOLD_YEAR}{m:02d}.json"
        if os.path.exists(path):
            return json.load(open(path))
        ym, last = f"{HOLD_YEAR}{m:02d}", calendar.monthrange(HOLD_YEAR, m)[1]
        key = "wl" if kind == "waterlevel" else "rf"
        for wait in (0, 3, 6):
            time.sleep(wait)
            try:
                j = cl.get(f"{base}/{kind}/list/1H/{code}/{ym}010000/{ym}{last:02d}2300.json").json()
                rows = [[x.get("ymdhm"), v1_num(x.get(key))] for x in (j.get("content") or [])]
                json.dump(rows, open(path, "w"))
                return rows
            except Exception:
                pass
        return []

    wl_info = json.load(open(f"{ROOT}/waterlevel_info.json"))
    rf_info = json.load(open(f"{ROOT}/rainfall_info.json"))
    me = next(x for x in wl_info if x.get("wlobscd") == HOLD_WL)
    p = (dms(me["lat"]), dms(me["lon"]))
    near = sorted((km(p, (dms(x["lat"]), dms(x["lon"]))), x["rfobscd"], x["obsnm"]) for x in rf_info
                  if dms(x.get("lat")) and dms(x.get("lon")))
    with httpx.Client(timeout=30) as cl:
        wl_rows = sum((month(cl, "waterlevel", HOLD_WL, m) for m in HOLD_MONTHS), [])
        best = None
        for d, code, name in near[:6]:
            if d > 15:
                break
            rows = sum((month(cl, "rainfall", code, m) for m in HOLD_MONTHS), [])
            n = sum(1 for _, v in rows if v is not None)
            log(f"동부교 짝 후보 {code} {name} {d:.1f}km: 2026 자료 {n}시간")
            if best is None or n > best[3]:
                best = (code, name, d, n, rows)
    if not best or best[3] == 0:
        return None, None
    code, name, d, n, rf_rows = best
    log(f"동부교 짝: {code} {name} ({d:.1f}km, {n}시간)")

    def series(rows):
        vals = {v1.to_time(t): v for t, v in rows if v is not None and v1.to_time(t) is not None}
        return pd.Series(vals, dtype="float32").sort_index()
    wl, rf = series(wl_rows), series(rf_rows)
    idx = pd.date_range(min(wl.index.min(), rf.index.min()), max(wl.index.max(), rf.index.max()), freq="h")
    df = pd.DataFrame({"wl": wl.reindex(idx), "rain": rf.reindex(idx)})
    df.loc[df["rain"] < 0, "rain"] = np.nan
    return df, {"rf": code, "rf_name": name, "km": round(d, 1)}


def v1_num(v):
    try:
        return float(str(v).strip())
    except ValueError:
        return None


# ---------------------------------------------------------------- 메인

def main():
    from sklearn.linear_model import LinearRegression
    import lightgbm as lgb

    pairs = json.load(open(f"{ROOT}/pairs.json"))
    rows, tables = [], []
    for i, pr in enumerate(pairs, 1):
        df = v1.station_frame(pr)
        if df is None:
            rows.append({**pr, "keep": False, "reason": "자료 없음"})
            continue
        m = v1.metrics(df)
        keep, reason = keep_station(m)
        if pr["wl"] == HOLD_WL:
            keep, reason = False, "동부교 — 최종 시험용 (학습 제외)"
        rows.append({**pr, **m, "keep": keep, "reason": reason})
        if keep:
            tables.append(table(df, pr["wl"]))
        log(f"[{i}/{len(pairs)}] {pr['wl']} {pr['wl_name']} keep={keep} {reason}")
    pd.DataFrame(rows).to_csv(f"{OUT}/stations.csv", index=False, encoding="utf-8-sig")
    kept = [r for r in rows if r["keep"]]
    log(f"관측소 {len(kept)}/{len(pairs)}곳 사용")

    data = pd.concat(tables)
    split = {"train": data[data["year"].isin(TRAIN_Y)], "valid": data[data["year"].isin(VALID_Y)],
             "test": data[data["year"].isin(TEST_Y)]}
    res = {"stations_used": [f"{r['wl']} {r['wl_name']}" for r in kept],
           "rows": {k: len(v) for k, v in split.items()},
           "target": f"앞으로 {HORIZON}시간 최고 수위 - 평소 수위(최근 1주 중앙값), 24시간 강수 1mm 이상인 시간만"}

    hold, hold_info = None, None
    try:
        hdf, hold_info = fetch_holdout()
        if hdf is not None:
            hold = table(hdf, HOLD_WL)
            hold = hold[hold.index.month.isin(HOLD_MONTHS)]
            log("동부교 2026 시험 줄 수:", len(hold))
    except Exception as e:
        log("동부교 받기 실패:", type(e).__name__, e)
    res["holdout"] = {"station": "1006664 원주시(동부교) 2026년 5~10월", "pair": hold_info,
                      "rows": 0 if hold is None else len(hold)}

    models = {}
    for name, cols in (("A_수위있음", RAIN_COLS + WL_COLS), ("B_강수만", RAIN_COLS)):
        tr, va = split["train"], split["valid"]
        lr = LinearRegression().fit(tr[cols], tr["target"])
        gb = lgb.LGBMRegressor(n_estimators=3000, learning_rate=0.03, num_leaves=31, min_child_samples=100,
                               subsample=0.8, subsample_freq=1, colsample_bytree=0.9, n_jobs=2, verbose=-1)
        gb.fit(tr[cols], tr["target"], eval_set=[(va[cols], va["target"])],
               callbacks=[lgb.early_stopping(100, verbose=False)])
        models[name] = (cols, lr, gb)
        for mname, mdl in (("linear", lr), ("lightgbm", gb)):
            r = {k: scores(split[k]["target"], mdl.predict(split[k][cols])) for k in ("valid", "test")}
            if hold is not None and len(hold):
                r["동부교_2026"] = scores(hold["target"], mdl.predict(hold[cols]))
            res.setdefault(name, {})[mname] = r
            log(name, mname, "test:", json.dumps(r["test"], ensure_ascii=False))
        gb.booster_.save_model(f"{OUT}/lgb_{name}.txt")

    cols, _, gb = models["B_강수만"]
    sample = split["test"].sample(min(20000, len(split["test"])), random_state=0)
    contrib = gb.predict(sample[cols], pred_contrib=True)[:, :-1]
    shap = pd.Series(np.abs(contrib).mean(axis=0), index=cols).sort_values(ascending=False)
    shap.rename("mean_abs_shap").to_csv(f"{OUT}/shap_B.csv", encoding="utf-8-sig")
    res["shap_B_강수만"] = {k: round(float(v), 4) for k, v in shap.items()}

    if hold is not None and len(hold):
        ev = hold[["rain_1h", "rain_6h", "rain_24h", "target"]].copy()
        for name, (cols, lr, gb) in models.items():
            ev[f"{name}_lgb"] = gb.predict(hold[cols])
        ev[ev["target"] >= 0.05].to_csv(f"{OUT}/holdout_events.csv", encoding="utf-8-sig")

    json.dump(res, open(f"{OUT}/results.json", "w"), ensure_ascii=False, indent=1)
    log("끝. 결과 →", f"{OUT}/results.json")


if __name__ == "__main__":
    main()
