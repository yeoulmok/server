"""행정안전부_물놀이_관리지역 (재난안전데이터 공유플랫폼 DSSP-IF-00186, #63).

  python -u -m app.safemap --probe      # 응답 모양만 보기 (키는 안 찍음)
  python -u -m app.safemap               # 원주·횡성·영월·강릉 관리지역 → 우리 21곳과 맞춰 보고 새 후보 + 관측소
  python -u -m app.safemap --merge       # 1.5km 안은 한 곳으로 묶고, 큰 강 본류 빼고, A/B 분류 → valleys.csv 줄
  python -u -m app.safemap --merge --all # 강원 전체 + 같은 강·같은 관측소는 자동으로 하나만 (#77)
키: ~/secrets/yeoulmok.env 의 SAFEMAP_KEY
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

URL = "https://www.safetydata.go.kr/V2/api/DSSP-IF-00186"
REGIONS = ("원주", "횡성", "영월", "강릉")
NEAR_KM = 1.5                     # 이 안에 우리 계곡이 있으면 "이미 있음"
OUT = Path("data/safemap_gangwon.csv")
R_EARTH = 6378137.0


def to_wgs84(x: float, y: float) -> tuple[float, float]:
    """웹 메르카토르(EPSG:3857) → 위도, 경도."""
    lon = x / R_EARTH * 180 / math.pi
    lat = (2 * math.atan(math.exp(y / R_EARTH)) - math.pi / 2) * 180 / math.pi
    return lat, lon


def items(payload: dict) -> tuple[list[dict], int]:
    head = payload.get("header") or {}
    if head.get("resultCode") not in (None, "00"):
        raise RuntimeError(f"물놀이 관리지역 {head.get('resultCode')}: {head.get('resultMsg')}")
    body = payload.get("body") or []
    return (body if isinstance(body, list) else [body]), int(payload.get("totalCount") or 0)


def pick(rows: list[dict], regions=REGIONS) -> list[dict]:
    """regions=None 이면 강원 전체."""
    return [r for r in rows if "강원" in (r.get("CTPV_NM") or "")
            and (regions is None or any(g in (r.get("SGG_NM") or "") for g in regions))]


def place_of(r: dict):
    from app import candidates as C

    lat, lon = to_wgs84(float(r["XMAP_CRTS"]), float(r["YMAP_CRTS"]))
    addr = (r.get("ADDR") or "").strip()
    tail = " ".join(addr.split()[-2:]) if addr else r.get("EMY_NM", "")
    return C.Place(f"관리지역 {r.get('SGG_NM', '')} {tail}".strip(), addr, lat, lon, f"safemap:{r.get('SN')}")


def fetch_all(client, key: str) -> list[dict]:
    out, page = [], 1
    while True:
        r = client.get(URL, params={"serviceKey": key, "pageNo": page, "numOfRows": 500, "returnType": "json"})
        r.raise_for_status()
        rows, total = items(json.loads(r.text))
        out += rows
        if not rows or page * 500 >= total:
            return out
        page += 1


def shape(x, depth=0, max_depth=4) -> list[str]:
    """JSON 구조만 (값은 짧게). 첫 항목만 펼침."""
    pad = "  " * depth
    out = []
    if isinstance(x, dict):
        for k, v in x.items():
            if isinstance(v, (dict, list)) and depth < max_depth:
                out.append(f"{pad}{k}: {type(v).__name__}{f'[{len(v)}]' if isinstance(v, list) else ''}")
                out += shape(v, depth + 1, max_depth)
            else:
                out.append(f"{pad}{k}: {str(v)[:60]!r}")
    elif isinstance(x, list) and x:
        out += shape(x[0], depth, max_depth)
    return out


def probe() -> None:
    import httpx

    key = os.environ.get("SAFEMAP_KEY")
    if not key:
        sys.exit("SAFEMAP_KEY 없음 → env 넣고 judge 다시 만들기")
    tries = [
        {"pageNo": 1, "numOfRows": 3, "returnType": "json"},
        {"pageNo": 1, "numOfRows": 3},
    ]
    with httpx.Client(timeout=30) as c:
        for extra in tries:
            r = c.get(URL, params={"serviceKey": key, **extra})
            print(f"\n== 파라미터 {list(extra)} → HTTP {r.status_code}, {r.headers.get('content-type')}")
            body = r.text.replace(key, "***")
            try:
                data = json.loads(body)
            except ValueError:
                print(body[:600])
                continue
            print("\n".join(shape(data)))
            break


MAIN_RIVERS = ("남한강", "동강", "서강", "평창강", "섬강", "한강", "북한강", "소양강", "홍천강")   # 큰 강 본류: 계곡 비 규칙이 안 맞음(댐·상류 방류) → 뺌
VALLEY_OUT = Path("data/safemap_valleys.csv")


@dataclass
class Spot:
    lat: float
    lon: float
    sgg: str
    emd: str
    addrs: list = field(default_factory=list)
    visitors: int = 0            # STOT 최대 (뜻 확인 전이라 참고만)
    accidents: int = 0           # WOWA_ACDNT_STOT 합
    risk: bool = False           # 위험구역 칸 있음
    actions: set = field(default_factory=set)


def _int(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def ri_of(addr: str) -> str:
    """주소에서 리 이름 (없으면 읍면). '강원특별자치도 영월군 김삿갓면 대야리 211-3' → '대야리'"""
    parts = addr.split()
    for w in parts[2:]:
        if w.endswith("리") and len(w) >= 2:
            return w
    return next((w for w in parts[2:] if w.endswith(("면", "읍"))), "")


def cluster(rows: list[dict], km_fn, within: float = NEAR_KM) -> list[Spot]:
    """가까운 지점끼리 한 곳으로 (처음 지점 기준)."""
    spots: list[Spot] = []
    for r in rows:
        lat, lon = to_wgs84(float(r["XMAP_CRTS"]), float(r["YMAP_CRTS"]))
        sp = next((s for s in spots if km_fn(lat, lon, s.lat, s.lon) <= within), None)
        if sp is None:
            sp = Spot(lat, lon, r.get("SGG_NM", ""), r.get("EMY_NM", ""))
            spots.append(sp)
        a = (r.get("ADDR") or "").strip()
        if a and a not in sp.addrs:
            sp.addrs.append(a)
        sp.visitors = max(sp.visitors, _int(r.get("STOT")))
        sp.accidents += _int(r.get("WOWA_ACDNT_STOT"))
        if (r.get("RSK_ZONE_SCTN") or "-") not in ("-", "", "0"):
            sp.risk = True
        if (r.get("SFTY_ACTN_MTTR") or "-") not in ("-", ""):
            sp.actions.add(r["SFTY_ACTN_MTTR"])
    return spots


@dataclass
class Cand:
    name: str
    sgg: str
    emd: str
    lat: float
    lon: float
    cls: str
    river: str
    rain: tuple            # (source, code, name, kind, km)
    water: tuple | None    # (code, name, km) — A만
    spot: Spot
    notes: list = field(default_factory=list)

    @property
    def key(self) -> tuple:
        """같은 강 + 같은 관측소면 같은 곳으로 봄 (A는 수위계, B는 비 관측소)."""
        return (self.river or self.name, self.water[0] if self.water else f"rain:{self.rain[1]}")

    @property
    def score(self) -> tuple:
        """남길 우선순위: 위험구역 > 사고 많음 > 관측소 가까움."""
        near = self.water[2] if self.water else self.rain[4]
        return (self.spot.risk, self.spot.accidents, -near)


def dedupe(cands: list[Cand], existing: list, km_fn, near_km: float = 4.0) -> tuple[list[Cand], list[str]]:
    """같은 강·같은 관측소 후보는 하나만, 이미 있는 계곡과 같은 관측소로 4km 안이면 뺌."""
    best: dict[tuple, Cand] = {}
    dropped = []
    for c in cands:
        dup = next((v for v in existing if km_fn(c.lat, c.lon, float(v.lat), float(v.lon)) <= near_km and (
            (c.water and v.water_code == c.water[0]) or (not c.water and not v.water_code and v.rain_code == c.rain[1]))), None)
        if dup:
            dropped.append(f"{c.name} — 이미 있는 '{dup.name}'와 같은 관측소")
            continue
        k = c.key
        if k not in best or c.score > best[k].score:
            if k in best:
                dropped.append(f"{best[k].name} — '{c.name}'와 같은 강·관측소")
            best[k] = c
        else:
            dropped.append(f"{c.name} — '{best[k].name}'와 같은 강·관측소")
    return list(best.values()), dropped


def merge(all_gangwon: bool = False) -> None:
    import httpx

    from app import candidates as C
    from app import valleys as V

    key = os.environ.get("SAFEMAP_KEY")
    if not key:
        sys.exit("SAFEMAP_KEY 없음")
    existing = V.load_csv()
    have = [(v.name, float(v.lat), float(v.lon)) for v in existing]
    names = {v.name for v in existing}
    with httpx.Client(timeout=30) as c:
        mine = pick(fetch_all(c, key), None if all_gangwon else REGIONS)
        allspots = cluster(mine, C.km)
        spots = [s for s in allspots if all(C.km(s.lat, s.lon, la, lo) > NEAR_KM for _, la, lo in have)]
        where = "강원 전체" if all_gangwon else ", ".join(REGIONS)
        print(f"{where} 관리지역 {len(mine)}개 → 1.5km 안 묶으니 {len(allspots)}곳, 우리 {len(have)}곳과 안 겹치는 곳 {len(spots)}곳", flush=True)
        stations = C.all_stations(c)
        geo, alive, rain_alive = C.Geo(c), C.alive_checker(c), C.alive_checker(c, "rainfall")
        cands, skipped = [], []
        for n, s in enumerate(spots, 1):
            if n % 10 == 0:
                print(f"  … {n}/{len(spots)} 분류 중", flush=True)
            vd = C.classify(s.lat, s.lon, stations, geo.basin, geo.river, alive, rain_alive)
            rv = vd.river[1] if vd.river else ""
            ri = ri_of(s.addrs[0]) if s.addrs else s.emd
            label = f"{s.sgg} {ri} ({rv or '하천 모름'})"
            if any(m in rv for m in MAIN_RIVERS):
                skipped.append(f"{label} — 큰 강 본류")
                continue
            if vd.cls == "C" or not vd.rain:
                skipped.append(f"{label} — 비 관측소 멂")
                continue
            r, w = vd.rain, vd.water
            water = (w[0].code, w[0].name, w[1]) if (w and vd.cls == "A") else None
            cands.append(Cand(f"{ri} {rv}".strip() if rv else f"{ri} 물놀이지역", s.sgg, s.emd, s.lat, s.lon, vd.cls, rv,
                              (r[0].source, r[0].code, r[0].name, r[0].kind, r[1]), water, s, list(vd.notes)))
    keep, dropped = dedupe(cands, existing, C.km)
    lines, cnt = [], {"A": 0, "B": 0}
    for k in sorted(keep, key=lambda x: (x.sgg, x.name)):
        name = k.name if k.name not in names else f"{k.name}({k.sgg[:2]})"
        names.add(name)
        cnt[k.cls] += 1
        sp = k.spot
        extra = f"사고 {sp.accidents}" + (", 위험구역" if sp.risk else "") + (f", {'/'.join(sorted(sp.actions))}" if sp.actions else "")
        note = (f"{k.cls} {'비+수위' if k.cls == 'A' else '비만'}. 행안부 물놀이 관리지역(#63) {extra}. 비 {k.rain[4]:.1f}km"
                + (f", 수위 {k.water[2]:.1f}km" if k.water else "") + f". 주소 {sp.addrs[0] if sp.addrs else ''}")
        print(f"[{k.cls}] {name}  ({k.sgg} {k.emd})  비 {k.rain[2]} {k.rain[4]:.1f}km"
              + (f"  수위 {k.water[1]} {k.water[2]:.1f}km" if k.water else "") + f"  | {extra}")
        lines.append([name, f"{k.sgg} {k.emd}", f"{k.lat:.6f}", f"{k.lon:.6f}", k.rain[0], k.rain[1], k.rain[2], k.rain[3],
                      k.water[0] if k.water else "", k.water[1] if k.water else "", "시범", note])
    print(f"\n뺀 곳 {len(skipped) + len(dropped)} (본류·관측소 멂 {len(skipped)}, 겹침 {len(dropped)})")
    for x in skipped + dropped:
        print(f"  - {x}")
    print(f"\n넣을 후보 A {cnt['A']} / B {cnt['B']}")
    VALLEY_OUT.parent.mkdir(parents=True, exist_ok=True)
    with VALLEY_OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "region", "lat", "lon", "rain_source", "rain_code", "rain_name", "rain_kind",
                    "water_code", "water_name", "status", "note"])
        w.writerows(lines)
    print(f"표: {VALLEY_OUT}")

EXTRA = ("STOT", "WOWA_ACDNT_STOT", "RSK_ZONE_SCTN", "MNG_RGN_DPCN_YN", "SFTY_ACTN_MTTR", "ETC")


def main() -> None:
    import httpx

    from app import candidates as C
    from app import valleys as V

    key = os.environ.get("SAFEMAP_KEY")
    if not key:
        sys.exit("SAFEMAP_KEY 없음")
    have = [(v.name, float(v.lat), float(v.lon)) for v in V.load_csv()]
    with httpx.Client(timeout=30) as c:
        rows = fetch_all(c, key)
        mine = pick(rows)
        print(f"전국 물놀이 관리지역 {len(rows)}곳 중 {', '.join(REGIONS)} {len(mine)}곳\n")
        new = []
        for r in mine:
            p = place_of(r)
            near = min(((n, C.km(p.lat, p.lon, la, lo)) for n, la, lo in have), key=lambda x: x[1])
            info = "  ".join(f"{k}={r.get(k)}" for k in EXTRA if r.get(k) not in (None, "", "-", "0"))
            tag = f"= {near[0]} ({near[1]:.1f}km)" if near[1] <= NEAR_KM else f"새 후보 (가장 가까운 우리 계곡 {near[0]} {near[1]:.1f}km)"
            print(f"- {p.addr}  {p.lat:.5f},{p.lon:.5f}  → {tag}" + (f"\n    {info}" if info else ""))
            if near[1] > NEAR_KM:
                new.append(p)
        print(f"\n우리 목록에 이미 있음 {len(mine) - len(new)}곳 / 새 후보 {len(new)}곳")
        if new:
            out = C.rows_for(new, C.all_stations(c))
            C.show(out)
            C.save(out, OUT)
            print(f"\n표: {OUT}")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    elif "--merge" in sys.argv:
        merge("--all" in sys.argv)
    else:
        main()
