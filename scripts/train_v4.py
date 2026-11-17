"""학습 실험 4차: 두 단계 알람 (규칙 → AI 확인).

3차 결과: 규칙24h는 사건을 안 놓치지만 헛경보가 많고, LightGBM은 헛경보가 적지만 가끔 놓친다.
→ 둘을 합친다.
  - 1단계 '주의' = 규칙24h (24시간 강수 ≥ X mm). 빨리, 놓치지 않게
  - 2단계 '경고' = 주의 중이면서 LightGBM 추정 상승폭 ≥ k × thr. 진짜 위험할 때만
  k(0.5 / 0.7 / 1.0)를 바꿔 가며 "놓친 사건 0을 유지하면서 헛경보가 얼마나 줄어드나"를 본다.
  비교: 규칙24h 단독 / 규칙72h 단독 / LightGBM 단독 (3차와 같은 기준)

평가 방식은 3차(train_v3.py)와 같음 (동부교 2026 사건 단위: 놓친 사건, 미리 알린 시간, 헛경보 묶음).

출력: data/train/out_v4/ (results.json, events.csv) + 로그에 요약표
실행:
  docker rm train3 >/dev/null 2>&1; docker run -d --name train4 --memory 2g --env-file ~/secrets/yeoulmok.env -v ~/yeoulmok:/app -w /app python:3.12-slim sh -c \
    "apt-get update -qq && apt-get install -y -qq libgomp1 >/dev/null && pip install -q numpy pandas scikit-learn lightgbm httpx && python -u scripts/train_v4.py >> data/train/train_v4.log 2>&1"
"""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import train_pipeline as v1  # noqa: E402
import train_v2 as v2  # noqa: E402
import train_v3 as v3  # noqa: E402

ROOT = v1.ROOT
OUT = f"{ROOT}/out_v4"
THRS = (0.1, 0.2, 0.3)
KS = (0.5, 0.7, 1.0)
os.makedirs(OUT, exist_ok=True)
log = v1.log


def main():
    import lightgbm as lgb

    pairs = json.load(open(f"{ROOT}/pairs.json"))
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

    hdf, hinfo = v2.fetch_holdout()
    hfull = v3.full_table(hdf, v2.HOLD_WL)
    hfull = hfull[hfull.index.month.isin(v2.HOLD_MONTHS)]
    ok = hfull.dropna(subset=["wl_above_base"])
    wet = ok["rain_24h"] >= 1
    log("동부교 짝:", hinfo, "평가 시간", len(ok))

    B = v2.RAIN_COLS
    gb = lgb.LGBMRegressor(n_estimators=3000, learning_rate=0.03, num_leaves=31, min_child_samples=100,
                           subsample=0.8, subsample_freq=1, colsample_bytree=0.9, n_jobs=2, verbose=-1)
    gb.fit(tr[B], tr["target"], eval_set=[(va[B], va["target"])], callbacks=[lgb.early_stopping(100, verbose=False)])
    est = pd.Series(gb.predict(ok[B]), index=ok.index)
    est_te = gb.predict(te[B])

    res = {"동부교_짝": hinfo, "rules": {}, "사건단위": {}, "시간단위_시험": {}}
    ev_rows = []
    for thr in THRS:
        r24 = v3.best_rule(tr["rain_24h"].values, tr["target"].values, thr)
        r72 = v3.best_rule(tr["rain_72h"].values, tr["target"].values, thr)
        res["rules"][f"{thr}m"] = {"24h_mm": v3.rnd(r24), "72h_mm": v3.rnd(r72)}
        caution = ok["rain_24h"] >= r24
        preds = {
            f"규칙24h(≥{r24:.0f}mm)": caution,
            f"규칙72h(≥{r72:.0f}mm)": ok["rain_72h"] >= r72,
            "LightGBM 단독": (est >= thr) & wet,
        }
        caution_te = te["rain_24h"] >= r24
        hour = {f"규칙24h(≥{r24:.0f}mm)": v3.cls(te["target"], caution_te, thr),
                "LightGBM 단독": v3.cls(te["target"], est_te >= thr, thr)}
        for k in KS:
            name = f"2단(규칙24h → AI≥{k * thr:.2f}m)"
            preds[name] = caution & (est >= k * thr)
            hour[name] = v3.cls(te["target"], caution_te.values & (est_te >= k * thr), thr)
        summ, rows = v3.event_eval(ok["wl_above_base"], preds, thr)
        res["사건단위"][f"{thr}m"] = summ
        res["시간단위_시험"][f"{thr}m"] = hour
        ev_rows += rows

    pd.DataFrame(ev_rows).to_csv(f"{OUT}/events.csv", index=False, encoding="utf-8-sig")
    json.dump(res, open(f"{OUT}/results.json", "w"), ensure_ascii=False, indent=1, default=str)

    log("===== 요약 =====")
    for thr in THRS:
        log(f"[{thr}m] 규칙 X값:", res["rules"][f"{thr}m"])
        log("  동부교 2026 사건단위")
        for name, s in res["사건단위"][f"{thr}m"].items():
            log(f"    {name:28s} 사건 {s['사건']}  놓침 {s['놓친 사건']}  헛경보 {s['헛경보 묶음']}  "
                f"미리 {s['미리 알린 시간(중앙값, h)']}h")
        log("  시험 2023~25 시간단위")
        for name, s in res["시간단위_시험"][f"{thr}m"].items():
            log(f"    {name:28s} 잡음 {s['잡음']}  맞춤 {s['맞춤']}  F1 {s['F1']}")
    log("끝. →", OUT)


if __name__ == "__main__":
    main()
