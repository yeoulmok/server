"""계곡 후보 뽑기 (#47) — 관광공사 목록 → 좌표 → 제일 가까운 관측소. DB는 안 건드림.

  python -m app.candidates 원주 횡성 영월        # 관광공사 '계곡' 검색 → 후보표 + data/candidates.csv
  python -m app.candidates --find 용수골계곡      # 관광공사에 없는 곳: 브이월드 검색 API로 이름 → 좌표
  python -m app.candidates --addr "원주시 판부면 서곡리 산 166"   # 브이월드 지오코더: 주소 → 좌표

나온 표를 보고 넣을 계곡을 고르면 db/valleys.csv에 한 줄씩 → python -m app.valleys apply.
관측소: HRFCO 강수(10분)·수위 + 기상청 AWS(1시간 누적). 키는 ~/secrets/yeoulmok.env
(HRFCO_KEY, KMA_KEY, TOUR_KEY, VWORLD_KEY).

주의
- '가장 가까운 관측소'는 직선거리일 뿐. 산 너머(다른 유역)일 수 있으니 지도로 한 번 보고 고르기
- 수위 관측소는 계곡 '하류'인지 거리만으론 모름 → 표에는 참고로만, 자동 연결 안 함
"""

from __future__ import annotations

import csv
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

TOUR_URL = "https://apis.data.go.kr/B551011/KorService2/searchKeyword2"
VWORLD_SEARCH = "https://api.vworld.kr/req/search"
VWORLD_GEOCODE = "https://api.vworld.kr/req/address"
HRFCO = "https://api.hrfco.go.kr"
KMA_STN = "https://apihub.kma.go.kr/api/typ01/url/stn_inf.php"
GANGWON = "51"                       # 법정동 시도코드 강원 (옛 areaCode=32는 예전 관광지가 빠짐, 11/24 확인)
OUT = Path(__file__).resolve().parents[1] / "data" / "candidates.csv"
NEAR_KM = 8.0                        # 이보다 멀면 '관측소 멂' 표시


@dataclass(frozen=True)
class Place:
    name: str
    addr: str
    lat: float
    lon: float
    source: str                      # tour:<contentid> / vworld


@dataclass(frozen=True)
class Station:
    source: str                      # hrfco / kma
    code: str
    name: str
    kind: str                        # rainfall / rainfall_1h / waterlevel
    lat: float
    lon: float


# ---------------------------------------------------------------------------
# 순수 계산
# ---------------------------------------------------------------------------

def km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def dms(s) -> float | None:
    """HRFCO '37-17-54' (도-분-초) 또는 '37.29' → 소수 도."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    parts = [p for p in re.split(r"[-\s:]+", s) if p]
    try:
        if len(parts) == 1:
            return float(parts[0])
        d, m, *rest = (float(p) for p in parts)
        return d + m / 60 + (rest[0] if rest else 0) / 3600
    except ValueError:
        return None


def nearest(p: Place, stations: list[Station], kinds: tuple[str, ...]) -> tuple[Station, float] | None:
    best = None
    for s in stations:
        if s.kind in kinds:
            d = km(p.lat, p.lon, s.lat, s.lon)
            if best is None or d < best[1]:
                best = (s, d)
    return best


def is_valley(title: str) -> bool:
    return "계곡" in title and not any(w in title for w in ("캠핑", "펜션", "야영", "식당", "가든", "민박"))


# ---------------------------------------------------------------------------
# 응답 읽기 (테스트하기 쉽게 분리)
# ---------------------------------------------------------------------------

def parse_tour(payload: dict) -> tuple[list[Place], int]:
    head = (payload.get("response") or {}).get("header") or {}
    if head.get("resultCode") not in (None, "0000"):
        raise ValueError(f"관광공사 오류 {head.get('resultCode')} {head.get('resultMsg')}")
    body = (payload.get("response") or {}).get("body") or {}
    items = (body.get("items") or {})
    items = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(items, dict):
        items = [items]
    out = []
    for it in items:
        try:
            lat, lon = float(it.get("mapy")), float(it.get("mapx"))
        except (TypeError, ValueError):
            continue
        out.append(Place(it.get("title", "").strip(), (it.get("addr1") or "").strip(), lat, lon,
                         f"tour:{it.get('contentid')}"))
    return out, int(body.get("totalCount") or 0)


def parse_hrfco(payload: dict, kind: str) -> list[Station]:
    code_key = "rfobscd" if kind == "rainfall" else "wlobscd"
    out = []
    for x in payload.get("content") or []:
        lat, lon = dms(x.get("lat")), dms(x.get("lon"))
        if lat and lon:
            out.append(Station("hrfco", str(x.get(code_key)), x.get("obsnm") or "", kind, lat, lon))
    return out


def parse_kma_stn(text: str) -> list[Station]:
    """기상청 지점 정보(텍스트 표). 열 순서가 바뀌어도 되게: 첫 숫자=지점번호, 한국 범위의 경도·위도, 첫 한글=이름."""
    out = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        toks = line.split()
        if not toks[0].isdigit():
            continue
        nums = []
        for t in toks[1:6]:
            try:
                nums.append(float(t))
            except ValueError:
                pass
        lon = next((v for v in nums if 124 < v < 132), None)
        lat = next((v for v in nums if 33 < v < 39), None)
        name = next((t for t in toks if re.search(r"[가-힣]", t)), toks[0])
        if lat and lon:
            out.append(Station("kma", toks[0], name, "rainfall_1h", lat, lon))
    return out


def parse_vworld_search(payload: dict) -> list[Place]:
    items = (((payload.get("response") or {}).get("result") or {}).get("items")) or []
    out = []
    for it in items:
        pt = it.get("point") or {}
        try:
            out.append(Place(it.get("title", ""), ((it.get("address") or {}).get("parcel") or ""),
                             float(pt["y"]), float(pt["x"]), "vworld"))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_vworld_geocode(payload: dict, addr: str) -> Place | None:
    pt = (((payload.get("response") or {}).get("result") or {}).get("point")) or {}
    try:
        return Place(addr, addr, float(pt["y"]), float(pt["x"]), "vworld")
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# API 호출
# ---------------------------------------------------------------------------

def _client():
    import httpx

    return httpx.Client(timeout=30)


def tour_valleys(regions: list[str], client) -> list[Place]:
    out, page = [], 1
    while True:
        r = client.get(TOUR_URL, params={
            "serviceKey": os.environ["TOUR_KEY"], "MobileOS": "ETC", "MobileApp": "yeoulmok", "_type": "json",
            "keyword": "계곡", "lDongRegnCd": GANGWON, "numOfRows": 100, "pageNo": page})
        r.raise_for_status()
        try:
            places, total = parse_tour(r.json())
        except ValueError as e:
            raise SystemExit(f"관광공사 응답 문제 ({e}): {r.text[:200]}")
        out += places
        if page * 100 >= total or not places:
            break
        page += 1
    print(f"  관광공사 강원 '계곡' 검색 {len(out)}건")
    return [p for p in out if is_valley(p.name) and any(reg in p.addr for reg in regions)]


def all_stations(client) -> list[Station]:
    st = []
    for kind in ("rainfall", "waterlevel"):
        r = client.get(f"{HRFCO}/{os.environ['HRFCO_KEY']}/{kind}/info.json")
        r.raise_for_status()
        st += parse_hrfco(r.json(), kind)
    try:
        r = client.get(KMA_STN, params={"inf": "AWS", "stn": "", "help": 0, "authKey": os.environ["KMA_KEY"]})
        r.raise_for_status()
        st += parse_kma_stn(r.content.decode("euc-kr", errors="replace"))
    except Exception as e:  # noqa: BLE001 — 기상청 목록을 못 받아도 HRFCO만으로 진행
        print(f"  (기상청 AWS 지점 목록 못 받음: {type(e).__name__} → HRFCO만)")
    return st


def vworld_find(name: str, client) -> list[Place]:
    r = client.get(VWORLD_SEARCH, params={"service": "search", "request": "search", "version": "2.0",
                                          "type": "place", "query": name, "size": 10, "format": "json",
                                          "key": os.environ["VWORLD_KEY"]})
    r.raise_for_status()
    return parse_vworld_search(r.json())


def vworld_geocode(addr: str, client) -> Place | None:
    for typ in ("PARCEL", "ROAD"):
        r = client.get(VWORLD_GEOCODE, params={"service": "address", "request": "getCoord", "type": typ,
                                               "address": addr, "format": "json", "key": os.environ["VWORLD_KEY"]})
        r.raise_for_status()
        p = parse_vworld_geocode(r.json(), addr)
        if p:
            return p
    return None


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

def rows_for(places: list[Place], stations: list[Station]) -> list[dict]:
    rows = []
    for p in places:
        rain_h = nearest(p, stations, ("rainfall",))
        rain_k = nearest(p, stations, ("rainfall_1h",))
        water = nearest(p, stations, ("waterlevel",))
        # 추천: 10분 자료(HRFCO)를 우선, 기상청이 절반 이하 거리로 확실히 가까우면 기상청
        pick = rain_h
        if rain_k and (rain_h is None or rain_k[1] < rain_h[1] * 0.5):
            pick = rain_k
        rows.append({"place": p, "pick": pick, "rain_h": rain_h, "rain_k": rain_k, "water": water})
    return sorted(rows, key=lambda r: r["pick"][1] if r["pick"] else 999)


def _st(x) -> str:
    return f"{x[0].name}({x[0].source}:{x[0].code}) {x[1]:.1f}km" if x else "-"


def show(rows: list[dict]) -> None:
    for r in rows:
        p, pick = r["place"], r["pick"]
        far = "  ⚠ 관측소 멂" if pick and pick[1] > NEAR_KM else ""
        print(f"\n■ {p.name}  ({p.addr})  {p.lat:.5f}, {p.lon:.5f}{far}")
        print(f"   추천 강수: {_st(pick)}")
        print(f"   HRFCO 강수 {_st(r['rain_h'])} / 기상청 AWS {_st(r['rain_k'])} / 가까운 수위(참고) {_st(r['water'])}")


def csv_line(r: dict) -> str:
    """db/valleys.csv에 그대로 붙일 수 있는 줄 (수위는 비움 → AI 판정)."""
    p, (s, d) = r["place"], r["pick"]
    region = " ".join(p.addr.split()[1:3]) if p.addr else ""
    return (f"{p.name},{region},{p.lat:.6f},{p.lon:.6f},{s.source},{s.code},{s.name},{s.kind},,,시범,"
            f"좌표={p.source}. 관측소 {d:.1f}km")


def save(rows: list[dict], path: Path = OUT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["계곡", "주소", "위도", "경도", "추천 강수", "거리(km)", "HRFCO 강수", "기상청 AWS",
                    "가까운 수위(참고)", "valleys.csv 줄"])
        for r in rows:
            p = r["place"]
            w.writerow([p.name, p.addr, f"{p.lat:.6f}", f"{p.lon:.6f}", _st(r["pick"]),
                        f"{r['pick'][1]:.1f}" if r["pick"] else "", _st(r["rain_h"]), _st(r["rain_k"]),
                        _st(r["water"]), csv_line(r) if r["pick"] else ""])


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return
    with _client() as c:
        if argv[0] == "--find":
            places = vworld_find(" ".join(argv[1:]), c)
        elif argv[0] == "--addr":
            p = vworld_geocode(" ".join(argv[1:]), c)
            places = [p] if p else []
        else:
            places = tour_valleys(argv, c)
            print(f"관광공사 '계곡' ({', '.join(argv)}): {len(places)}곳")
        if not places:
            print("찾은 곳 없음")
            return
        rows = rows_for(places, all_stations(c))
    show(rows)
    if argv[0] not in ("--find", "--addr"):
        save(rows)
        print(f"\n표: {OUT}  (마지막 열이 db/valleys.csv에 붙일 줄)")
    else:
        print("\nvalleys.csv 줄:")
        for r in rows:
            if r["pick"]:
                print("  " + csv_line(r))


if __name__ == "__main__":
    main()
