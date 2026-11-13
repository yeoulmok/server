"""학습용 과거 자료 수집 (강원 수위관측소 + 가까운 우량관측소, 2010~2025년 6~9월, 1시간 자료)

- 받은 자료는 data/train/raw/ 에 월 단위 파일로 저장 -> 끊겨도 다시 실행하면 이어서 받음
- 실행: docker exec -d collector sh -c "python -u scripts/fetch_train.py >> data/train/fetch.log 2>&1"
"""
import os, sys, json, time, math, calendar
import httpx

HK = os.environ["HRFCO_KEY"]
BASE = "https://api.hrfco.go.kr/" + HK
OUT = "/app/data/train"
RAW = OUT + "/raw"
YEARS = range(2010, 2026)
MONTHS = (6, 7, 8, 9)
SCREEN = (2015, 7)          # 이 달 자료가 25일 이상인 관측소만 씀
RAIN_MAX_KM = 15            # 수위관측소에서 이 거리 안의 우량관측소만 짝지음
KEY = {"waterlevel": "wl", "rainfall": "rf"}
CODE = {"waterlevel": "wlobscd", "rainfall": "rfobscd"}

os.makedirs(RAW, exist_ok=True)


def log(*a):
    print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)


def num(v):
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def dms(s):
    try:
        d, m, sec = [float(p) for p in str(s).strip().split("-")]
        return d + m / 60 + sec / 3600
    except Exception:
        return None


def km(a, b):
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    x = (lo2 - lo1) * math.cos((la1 + la2) / 2)
    y = la2 - la1
    return 6371 * math.hypot(x, y)


def get_json(cl, url):
    for wait in (0, 3, 6):
        time.sleep(wait)
        try:
            return cl.get(url).json()
        except Exception:
            pass
    return None


def info(cl, kind):
    path = f"{OUT}/{kind}_info.json"
    if os.path.exists(path):
        return json.load(open(path))
    for scheme in ("https", "http"):
        try:
            r = cl.get(f"{scheme}://api.hrfco.go.kr/{HK}/{kind}/info.json",
                       headers={"Accept-Encoding": "gzip"}, timeout=60)
            c = r.json().get("content") or []
            if c:
                json.dump(c, open(path, "w"), ensure_ascii=False)
                log(kind, "목록", len(c), "곳 저장")
                return c
        except Exception as e:
            log(kind, "목록 실패", scheme, type(e).__name__)
        time.sleep(3)
    sys.exit(kind + " 목록을 못 받음")


def span(cl, kind, code, s, e):
    j = get_json(cl, f"{BASE}/{kind}/list/1H/{code}/{s}/{e}.json")
    if j is None:
        return None
    return [[x.get("ymdhm"), num(x.get(KEY[kind]))] for x in (j.get("content") or [])]


def month(cl, kind, code, y, m):
    """한 달치 1시간 자료. 저장된 파일이 있으면 그걸 씀. 한 달이 실패하면 반달씩 나눠 받음."""
    path = f"{RAW}/{kind}_{code}_{y}{m:02d}.json"
    if os.path.exists(path):
        return json.load(open(path)), False
    last = calendar.monthrange(y, m)[1]
    ym = f"{y}{m:02d}"
    rows = span(cl, kind, code, ym + "010000", f"{ym}{last:02d}2300")
    if rows is None:
        time.sleep(2)
        r1 = span(cl, kind, code, ym + "010000", ym + "152300")
        time.sleep(1)
        r2 = span(cl, kind, code, ym + "160000", f"{ym}{last:02d}2300")
        if r1 is None or r2 is None:
            return None, True
        rows = r1 + r2
    json.dump(rows, open(path, "w"))
    return rows, True


def days(rows):
    return len({t[:8] for t, v in (rows or []) if t and v is not None})


def main():
    with httpx.Client(timeout=30) as cl:
        wl = info(cl, "waterlevel")
        rf = info(cl, "rainfall")

        wl = [x for x in wl if "강원" in (x.get("addr") or "") and "댐" not in (x.get("obsnm") or "")]
        rpts = []
        for x in rf:
            p = (dms(x.get("lat")), dms(x.get("lon")))
            if None not in p:
                rpts.append((x.get(CODE["rainfall"]), x.get("obsnm"), p))

        # 1) 수위관측소 선별 + 가까운 우량관측소 짝짓기
        pairs_path = f"{OUT}/pairs.json"
        if os.path.exists(pairs_path):
            pairs = json.load(open(pairs_path))
            log("저장된 짝 목록 사용:", len(pairs), "곳")
        else:
            pairs, screen_fail = [], 0
            y0, m0 = SCREEN
            for i, x in enumerate(wl, 1):
                code, name = x.get(CODE["waterlevel"]), x.get("obsnm")
                rows, fresh = month(cl, "waterlevel", code, y0, m0)
                if fresh:
                    time.sleep(1)
                if rows is None:
                    screen_fail += 1
                    log(f"[선별 {i}/{len(wl)}] {code} {name} 받기 실패")
                    continue
                if days(rows) < 25:
                    log(f"[선별 {i}/{len(wl)}] {code} {name} 제외: {y0}-{m0:02d} 자료 {days(rows)}일")
                    continue
                p = (dms(x.get("lat")), dms(x.get("lon")))
                if None in p:
                    log(f"[선별 {i}/{len(wl)}] {code} {name} 제외: 좌표 없음")
                    continue
                near = sorted((km(p, q[2]), q[0], q[1]) for q in rpts)
                pick = None
                for d, rcode, rname in near[:3]:
                    if d > RAIN_MAX_KM:
                        break
                    rrows, fresh = month(cl, "rainfall", rcode, y0, m0)
                    if fresh:
                        time.sleep(1)
                    if days(rrows) >= 25:
                        pick = (d, rcode, rname)
                        break
                if pick is None:
                    log(f"[선별 {i}/{len(wl)}] {code} {name} 제외: {RAIN_MAX_KM}km 안에 쓸 만한 우량관측소 없음")
                    continue
                d, rcode, rname = pick
                pairs.append({"wl": code, "wl_name": name, "addr": x.get("addr"),
                              "lat": p[0], "lon": p[1],
                              "rf": rcode, "rf_name": rname, "km": round(d, 1)})
                log(f"[선별 {i}/{len(wl)}] {code} {name} <- {rcode} {rname} ({d:.1f}km)")
            if screen_fail:
                log(f"선별 중 받기 실패 {screen_fail}곳 -> 짝 목록 저장 안 함. 다시 실행하면 실패한 곳만 다시 봄")
                return
            json.dump(pairs, open(pairs_path, "w"), ensure_ascii=False, indent=1)
            log("짝 목록 저장:", len(pairs), "곳 ->", pairs_path)

        # 2) 본 수집
        todo = set()
        for pr in pairs:
            for y in YEARS:
                for m in MONTHS:
                    todo.add(("waterlevel", pr["wl"], y, m))
                    todo.add(("rainfall", pr["rf"], y, m))
        left = [t for t in todo if not os.path.exists(f"{RAW}/{t[0]}_{t[1]}_{t[2]}{t[3]:02d}.json")]
        log(f"수집 대상 {len(todo)}개 중 남은 것 {len(left)}개 (예상 {len(left) * 2.5 / 3600:.1f}시간)")

        fails = []
        for k, pr in enumerate(pairs, 1):
            for y in YEARS:
                got = 0
                for m in MONTHS:
                    for kind, code in (("waterlevel", pr["wl"]), ("rainfall", pr["rf"])):
                        rows, fresh = month(cl, kind, code, y, m)
                        if fresh:
                            time.sleep(1)
                        if rows is None:
                            fails.append([kind, code, y, m])
                        else:
                            got += 1
                log(f"[{k}/{len(pairs)}] {pr['wl']} {pr['wl_name']} {y}: {got}/8")
        json.dump(fails, open(f"{OUT}/fails.json", "w"))
        log(f"끝. 실패 {len(fails)}개 (다시 실행하면 실패한 것만 다시 받음)")


if __name__ == "__main__":
    main()
