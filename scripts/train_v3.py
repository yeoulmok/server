"""학습 실험 3차: "AI가 단순 규칙보다 나은가" + "사건마다 몇 시간 전에 알렸나".

2차(train_v2.py)와 같은 관측소·정답(앞으로 3시간 최고 수위 - 평소 수위)을 쓰고, 비교 대상을 늘린다.
  - 규칙 24h : "24시간 강수 ≥ X mm면 경보"  (X는 학습 기간에서 가장 잘 맞는 값으로 자동 결정)
  - 규칙 72h : "72시간 강수 ≥ X mm면 경보"
  - 선형회귀 (강수만) / LightGBM (강수만)
  - (참고) 수위 있음 모델 A와 '지금 수위 그대로' 비교 → A가 그냥 현재 수위를 따라 하는 건지 확인

동부교 2026 사건 평가
  - 사건 = 실제로 평소보다 thr 이상 차오른 구간 (6시간 안에 다시 차오르면 같은 사건)
  - 차오른 시각(Tc)보다 최대 12시간 전부터 경보가 처음 울린 시각(Tw) → 미리 알린 시간 = Tc - Tw
  - 미리 알린 시간이 12면 "12시간 이상 전부터 이미 울리고 있었음", 음수면 "차오른 뒤에 늦게 울림"
  - 헛경보 = 사건 구간(앞 12시간 ~ 끝 3시간)과 겹치지 않는 경보 묶음 수

출력: data/train/out_v3/ (results.json, events.csv) + 로그에 요약표
실행:
  docker rm train2 >/dev/null 2>&1; docker run -d --name train3 --memory 2g --env-file ~/secrets/yeoulmok.env -v ~/yeoulmok:/app -w /app python:3.12-slim sh -c \
    "apt-get update -qq && apt-get install -y -qq libgomp1 >/dev/null && pip install -q numpy pandas scikit-learn lightgbm httpx && python -u scripts/train_v3.py >> data/train/train_v3.log 2>&1"
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import train_pipeline as v1  # noqa: E402
import train_v2 as v2  # noqa: E402

ROOT = v1.ROOT
OUT = f"{ROOT}/out_v3"
THRS = (0.1, 0.2, 0.3)
EVENT_THRS = (0.1, 0.3)
LEAD_WINDOW = 12          # 차오르기 몇 시간 전까지의 경보를 "미리 알림"으로 칠지
MERGE_GAP = 6             # 이 시간 안에 다시 차오르면 같은 사건
os.makedirs(OUT, exist_ok=True)
log = v1.log


def full_table(df, code):
    """v2.table과 같은 특징·정답, 단 '비 온 시간만' 필터 없이 (사건 평가용)."""
    f = pd.DataFrame(index=df.index)
    for w in (1, 3, 6, 12, 24, 72):
        f[f"rain_{w}h"] = df["rain"].rolling(w, min_periods=w).sum()
    f["rain_max1h_6h"] = df["rain"].rolling(6, min_periods=6).max()
    base = df["wl"].rolling(24 * 7, min_periods=24).median()
    f["wl_above_base"] = df["wl"] - base
    f["wl_change_1h"] = df["wl"].diff(1)
    f["wl_change_3h"] = df["wl"].diff(3)
    f["month"] = df.index.month
    fut = df["wl"].shift(-v2.HORIZON).rolling(v2.HORIZON, min_periods=v2.HORIZON).max()
    f["target"] = fut - base
    f["station"] = code
    return f.dropna(subset=[c for c in v2.RAIN_COLS] + ["wl_above_base"])


def cls(y, hit, thr):
    t = np.asarray(y) >= thr
    h = np.asarray(hit, dtype=bool)
    tp = int((t & h).sum())
    rec = tp / t.sum() if t.sum() else None
    pre = tp / h.sum() if h.sum() else None
    f1 = 2 * rec * pre / (rec + pre) if rec and pre else None
    return {"실제": int(t.sum()), "잡음": rnd(rec), "맞춤": rnd(pre), "F1": rnd(f1)}


def rnd(x):
    return None if x is None else round(float(x), 3)


def best_rule(x, y, thr):
    """학습 기간에서 F1이 가장 높은 강수 문턱값 X."""
    cands = np.unique(np.percentile(x, np.linspace(1, 99, 99)))
    best = (None, -1)
    t = y >= thr
    for c in cands:
        h = x >= c
        tp = (t & h).sum()
        if tp == 0:
            continue
        rec, pre = tp / t.sum(), tp / h.sum()
        f1 = 2 * rec * pre / (rec + pre)
        if f1 > best[1]:
            best = (float(c), f1)
    return best[0]


def alarm_groups(flags):
    """True가 이어진 구간들의 (시작, 끝) 시각."""
    groups, start, prev = [], None, None
    for ts, v in flags.items():
        if v and start is None:
            start = ts
        if not v and start is not None:
            groups.append((start, prev))
            start = None
        prev = ts
    if start is not None:
        groups.append((start, prev))
    return groups


def events_of(obs_above, thr):
    """실제로 평소보다 thr 이상 차오른 사건들 (MERGE_GAP 안이면 합침). (시작, 끝, 최고)"""
    gs = alarm_groups(obs_above >= thr)
    merged = []
    for s, e in gs:
        if merged and (s - merged[-1][1]) <= pd.Timedelta(hours=MERGE_GAP):
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return [(s, e, float(obs_above[s:e].max())) for s, e in merged]


def event_eval(obs_above, preds, thr):
    evs = events_of(obs_above, thr)
    rows, summary = [], {}
    for name, flag in preds.items():
        leads, missed = [], 0
        for s, e, peak in evs:
            win = flag[s - pd.Timedelta(hours=LEAD_WINDOW): e]
            first = win[win].index.min() if win.any() else None
            lead = (s - first).total_seconds() / 3600 if first is not None else None
            if lead is None:
                missed += 1
            else:
                leads.append(lead)
            rows.append({"thr": thr, "model": name, "event_start": s, "event_end": e, "peak_above_base_m": round(peak, 2),
                         "first_alarm": first, "lead_h": lead})
        cover = [(s - pd.Timedelta(hours=LEAD_WINDOW), e + pd.Timedelta(hours=3)) for s, e, _ in evs]
        false = sum(1 for a, b in alarm_groups(flag) if not any(a <= ce and b >= cs for cs, ce in cover))
        summary[name] = {"사건": len(evs), "잡은 사건": len(evs) - missed, "놓친 사건": missed,
                         "미리 알린 시간(중앙값, h)": rnd(np.median(leads)) if leads else None,
                         "헛경보 묶음": false}
    return summary, rows


def main():
    from sklearn.linear_model import LinearRegression
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
    hfull = full_table(hdf, v2.HOLD_WL)
    hfull = hfull[hfull.index.month.isin(v2.HOLD_MONTHS)]
    hrain = hfull[hfull["rain_24h"] >= 1].dropna(subset=["target"])
    log("동부교 짝:", hinfo, "전체 시간", len(hfull), "비 온 시간", len(hrain))

    B = v2.RAIN_COLS
    lr = LinearRegression().fit(tr[B], tr["target"])
    gb = lgb.LGBMRegressor(n_estimators=3000, learning_rate=0.03, num_leaves=31, min_child_samples=100,
                           subsample=0.8, subsample_freq=1, colsample_bytree=0.9, n_jobs=2, verbose=-1)
    gb.fit(tr[B], tr["target"], eval_set=[(va[B], va["target"])], callbacks=[lgb.early_stopping(100, verbose=False)])
    A = v2.RAIN_COLS + v2.WL_COLS
    gbA = lgb.LGBMRegressor(n_estimators=3000, learning_rate=0.03, num_leaves=31, min_child_samples=100,
                            subsample=0.8, subsample_freq=1, colsample_bytree=0.9, n_jobs=2, verbose=-1)
    gbA.fit(tr[A], tr["target"], eval_set=[(va[A], va["target"])], callbacks=[lgb.early_stopping(100, verbose=False)])

    res = {"동부교_짝": hinfo, "rules": {}, "시간단위": {}, "사건단위": {}, "A_참고": {}}
    ev_rows = []
    for thr in THRS:
        r24 = best_rule(tr["rain_24h"].values, tr["target"].values, thr)
        r72 = best_rule(tr["rain_72h"].values, tr["target"].values, thr)
        res["rules"][f"{thr}m"] = {"24h_mm": rnd(r24), "72h_mm": rnd(r72)}
        out = {}
        for split, d in (("시험_2023_25", te), ("동부교_2026", hrain)):
            out[split] = {
                f"규칙24h(≥{r24:.0f}mm)": cls(d["target"], d["rain_24h"] >= r24, thr),
                f"규칙72h(≥{r72:.0f}mm)": cls(d["target"], d["rain_72h"] >= r72, thr),
                "선형(강수만)": cls(d["target"], lr.predict(d[B]) >= thr, thr),
                "LightGBM(강수만)": cls(d["target"], gb.predict(d[B]) >= thr, thr),
            }
        res["시간단위"][f"{thr}m"] = out
        res["A_참고"][f"{thr}m"] = {"지금수위그대로": cls(te["target"], te["wl_above_base"] >= thr, thr),
                                   "LightGBM_A": cls(te["target"], gbA.predict(te[A]) >= thr, thr)}

        if thr in EVENT_THRS:
            ok = hfull.dropna(subset=["wl_above_base"])
            wet = ok["rain_24h"] >= 1
            preds = {
                "규칙24h": (ok["rain_24h"] >= r24),
                "규칙72h": (ok["rain_72h"] >= r72),
                "선형(강수만)": pd.Series(lr.predict(ok[B]) >= thr, index=ok.index) & wet,
                "LightGBM(강수만)": pd.Series(gb.predict(ok[B]) >= thr, index=ok.index) & wet,
            }
            summ, rows = event_eval(ok["wl_above_base"], preds, thr)
            res["사건단위"][f"{thr}m"] = summ
            ev_rows += rows

    pd.DataFrame(ev_rows).to_csv(f"{OUT}/events.csv", index=False, encoding="utf-8-sig")
    json.dump(res, open(f"{OUT}/results.json", "w"), ensure_ascii=False, indent=1, default=str)

    log("===== 요약 =====")
    for thr in THRS:
        log(f"[{thr}m 시간단위] 규칙 X값:", res["rules"][f"{thr}m"])
        for split, d in res["시간단위"][f"{thr}m"].items():
            for name, s in d.items():
                log(f"  {split:12s} {name:18s} 잡음 {s['잡음']}  맞춤 {s['맞춤']}  F1 {s['F1']}")
        for name, s in res["A_참고"][f"{thr}m"].items():
            log(f"  (참고A) {name:14s} 잡음 {s['잡음']}  맞춤 {s['맞춤']}  F1 {s['F1']}")
    for k, summ in res["사건단위"].items():
        log(f"[{k} 동부교 2026 사건단위]")
        for name, s in summ.items():
            log(f"  {name:18s} {s}")
    log("끝. →", OUT)


if __name__ == "__main__":
    main()
