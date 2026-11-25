"""행정안전부_물놀이_관리지역 (재난안전데이터 공유플랫폼 DSSP-IF-00186, #63).

  python -u -m app.safemap --probe      # 응답 모양만 보기 (키는 안 찍음)
  python -u -m app.safemap               # 원주·횡성·영월·강릉 관리지역 → 우리 21곳과 맞춰 보고 새 후보 + 관측소
키: ~/secrets/yeoulmok.env 의 SAFEMAP_KEY
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
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
    return [r for r in rows if "강원" in (r.get("CTPV_NM") or "") and any(g in (r.get("SGG_NM") or "") for g in regions)]


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
    else:
        main()
