"""계곡 후보 뽑기 (#47) — 관광공사 목록 → 좌표 → 제일 가까운 관측소. DB는 안 건드림.

  python -m app.candidates 원주 횡성 영월        # 관광공사 '계곡' 검색 → 후보표 + data/candidates.csv
  python -m app.candidates --find 용수골계곡      # 관광공사에 없는 곳: 브이월드 검색 API로 이름 → 좌표
  python -m app.candidates --addr "원주시 판부면 서곡리 산 166"   # 브이월드 지오코더: 주소 → 좌표
  python -m app.candidates --classify          # db/valleys.csv 전부 A/B/C 분류 (표준유역·하천·수위 자료 확인)
  python -m app.candidates --scan 원주 횡성 영월 강릉   # 더 넓게: 관광공사 관광지 전체 훑기 + 브이월드 지명 '계곡' (#63)

분류 (#52)
  A 비+수위: 같은 표준유역 안에 수위계가 있고 최근 자료가 들어옴 → 규칙만으로 판정 (AI는 수위 결측 때만)
  B 비만   : 같은 유역 수위계가 없음 → 비 규칙 + 예측 AI
  C 보류   : 같은 유역에 비 관측소도 없고 가장 가까운 비 관측소가 8km 넘음
  표준유역·하천 이름은 브이월드 2D데이터(LT_C_WKMSBSN 표준유역, LT_C_WKMSTRM 하천망).
  11/24 확인: 금대리·동부교 둘 다 100606 원주천 → 이미 아는 정답과 같음.

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
TOUR_AREA_URL = "https://apis.data.go.kr/B551011/KorService2/areaBasedList2"
VALLEY_CAT3 = "A01010900"            # 관광공사 옛 분류: 자연 > 자연관광지 > 계곡
REGION_BBOX = {                      # 브이월드 검색 범위 (대략, 주소로 한 번 더 거름)
    "원주": (127.72, 37.17, 128.22, 37.52), "횡성": (127.78, 37.33, 128.38, 37.72),
    "영월": (128.08, 37.03, 128.82, 37.42), "강릉": (128.58, 37.48, 129.12, 37.98),
}
SAME_KM = 1.0                        # 이 거리 안이면 같은 곳으로 봄
VWORLD_SEARCH = "https://api.vworld.kr/req/search"
VWORLD_GEOCODE = "https://api.vworld.kr/req/address"
HRFCO = "https://api.hrfco.go.kr"
KMA_STN = "https://apihub.kma.go.kr/api/typ01/url/stn_inf.php"
VWORLD_DATA = "https://api.vworld.kr/req/data"
VWORLD_DOMAIN = "https://github.com/yeoulmok/server"
SEARCH_KM = 12.0                     # 분류할 때 볼 관측소 반경
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


# ---------------------------------------------------------------------------
# 넓게 훑기 (#63)
# ---------------------------------------------------------------------------

WATER_WORDS = ("계곡", "유원지", "골짜기", "폭포", "소(沼)")
NOT_WATER = ("캠핑", "펜션", "야영", "식당", "가든", "민박", "카페", "모텔", "리조트", "글램핑", "농원", "휴게소", "주차장",
             "보존회", "협회", "마을회관", "교회", "산장")


def looks_like_valley(title: str, cat3: str = "") -> bool:
    if any(w in title for w in NOT_WATER):
        return False
    return cat3 == VALLEY_CAT3 or any(w in title for w in WATER_WORDS)


def parse_tour_area(payload: dict) -> tuple[list[tuple[Place, str]], int]:
    """관광지 전체 목록 → [(Place, cat3)]"""
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
        out.append((Place(it.get("title", "").strip(), (it.get("addr1") or "").strip(), lat, lon,
                          f"tour:{it.get('contentid')}"), it.get("cat3") or ""))
    return out, int(body.get("totalCount") or 0)


def dedupe(places: list[Place], existing: list[tuple[str, float, float]]) -> list[Place]:
    """이름이 같거나 1km 안이면 하나로 (이미 목록에 있는 계곡도 뺌)."""
    keep: list[Place] = []
    norm = lambda n: re.sub(r"[\s()·]|원주|횡성|영월|강릉|일원", "", n)   # noqa: E731
    for p in places:
        if any(norm(p.name) == norm(n) or km(p.lat, p.lon, la, lo) < SAME_KM for n, la, lo in existing):
            continue
        if any(norm(p.name) == norm(q.name) or km(p.lat, p.lon, q.lat, q.lon) < SAME_KM for q in keep):
            continue
        keep.append(p)
    return keep


def tour_scan(regions: list[str], client) -> list[Place]:
    out, page, total = [], 1, None
    while True:
        r = client.get(TOUR_AREA_URL, params={
            "serviceKey": os.environ["TOUR_KEY"], "MobileOS": "ETC", "MobileApp": "yeoulmok", "_type": "json",
            "lDongRegnCd": GANGWON, "contentTypeId": 12, "numOfRows": 500, "pageNo": page})
        r.raise_for_status()
        rows, total = parse_tour_area(r.json())
        out += [(p, c) for p, c in rows if any(reg in p.addr for reg in regions)]
        if page * 500 >= total or not rows:
            break
        page += 1
    print(f"  관광공사 강원 관광지 {total}건 중 {', '.join(regions)} {len(out)}건 훑음")
    return [p for p, c in out if looks_like_valley(p.name, c)]


def vworld_scan(regions: list[str], client) -> list[Place]:
    out = []
    for reg in regions:
        bbox = REGION_BBOX.get(reg)
        if not bbox:
            continue
        for word in ("계곡", "유원지"):
            for page in (1, 2, 3):
                r = client.get(VWORLD_SEARCH, params={"service": "search", "request": "search", "version": "2.0",
                                                      "type": "place", "query": f"{reg} {word}", "size": 100, "page": page,
                                                      "bbox": ",".join(map(str, bbox)), "format": "json",
                                                      "key": os.environ["VWORLD_KEY"]})
                r.raise_for_status()
                got = parse_vworld_search(r.json())
                out += [p for p in got if reg in p.addr and looks_like_valley(p.name)]
                if len(got) < 100:
                    break
    return out


def scan(regions: list[str]) -> None:
    from app import valleys as V

    have = [(v.name, float(v.lat), float(v.lon)) for v in V.load_csv()]
    with _client() as c:
        a = tour_scan(regions, c)
        try:
            b = vworld_scan(regions, c)
        except Exception as e:  # noqa: BLE001
            print(f"  브이월드 검색 실패 ({type(e).__name__}) → 관광공사만")
            b = []
        print(f"  관광공사에서 {len(a)}곳, 브이월드 지명에서 {len(b)}곳 (겹침·이미 있는 곳 빼기 전)")
        new = dedupe(a + b, have)
        print(f"새 후보 {len(new)}곳 (지금 목록 {len(have)}곳과 1km 안·같은 이름은 뺌)")
        if not new:
            return
        rows = rows_for(new, all_stations(c))
    show(rows)
    save(rows, OUT.parent / "candidates_scan.csv")
    print(f"\n표: {OUT.parent / 'candidates_scan.csv'}  (마지막 열이 db/valleys.csv 줄)")


# ---------------------------------------------------------------------------
# 분류 (#52): 표준유역 · 하천 · 수위 자료
# ---------------------------------------------------------------------------

def parse_vworld_feature(payload: dict, field_code: str, field_name: str) -> tuple[str, str] | None:
    res = payload.get("response") or {}
    feats = ((res.get("result") or {}).get("featureCollection") or {}).get("features") or []
    if not feats:
        return None
    pr = feats[0].get("properties") or {}
    return (str(pr.get(field_code) or ""), str(pr.get(field_name) or ""))


class Geo:
    """브이월드 표준유역·하천 조회 (같은 좌표는 한 번만)."""

    def __init__(self, client):
        self.c, self.cache = client, {}

    def _get(self, layer, lat, lon, buffer):
        k = (layer, round(lat, 5), round(lon, 5), buffer)
        if k not in self.cache:
            r = self.c.get(VWORLD_DATA, params={"service": "data", "request": "GetFeature", "data": layer,
                                                "key": os.environ["VWORLD_KEY"], "domain": VWORLD_DOMAIN,
                                                "format": "json", "size": 1, "geometry": "false",
                                                "geomFilter": f"POINT({lon} {lat})", "buffer": buffer})
            r.raise_for_status()
            self.cache[k] = r.json()
        return self.cache[k]

    def basin(self, lat, lon):
        """(표준유역 코드, 이름) 또는 None"""
        return parse_vworld_feature(self._get("LT_C_WKMSBSN", lat, lon, 1), "sbsncd", "sbsnnm")

    def river(self, lat, lon):
        """가까운 하천 (코드 자리에 등급, 이름) 또는 None — 작은 계곡은 하천망에 없을 수 있음"""
        return parse_vworld_feature(self._get("LT_C_WKMSTRM", lat, lon, 500), "cat_nam", "riv_nm")


@dataclass
class Verdict:
    cls: str                          # A / B / C
    rain: tuple | None                # (Station, km, 같은유역?)
    water: tuple | None               # (Station, km, 하천이름, 최근자료칸수)
    basin: tuple | None
    river: tuple | None
    notes: list


def classify(lat, lon, stations, basin_of, river_of, alive_of, rain_alive_of=lambda code: 1) -> Verdict:
    """순수 판단 (조회 함수는 밖에서 넣음 → 테스트 쉬움)."""
    vb = basin_of(lat, lon)
    vr = river_of(lat, lon)
    notes = []
    near = sorted(((s, km(lat, lon, s.lat, s.lon)) for s in stations), key=lambda x: x[1])
    near = [(s, d) for s, d in near if d <= SEARCH_KM]
    same = lambda s: vb is not None and basin_of(s.lat, s.lon) == vb   # noqa: E731

    rains = [(s, d) for s, d in near if s.kind in ("rainfall", "rainfall_1h")]
    dead = [(s, d) for s, d in rains if s.source == "hrfco" and rain_alive_of(s.code) == 0]
    for s, _ in dead:
        notes.append(f"비 관측소 {s.name} 최근 자료 없음 → 뺌")
    rains = [x for x in rains if x not in dead]
    in_basin = [(s, d) for s, d in rains if same(s)]
    pool = in_basin or rains
    rain = None
    if pool:
        hr = next(((s, d) for s, d in pool if s.kind == "rainfall"), None)
        km_ = next(((s, d) for s, d in pool if s.kind == "rainfall_1h"), None)
        pick = hr if hr else km_
        if km_ and hr and km_[1] < hr[1] * 0.5:
            pick = km_
        rain = (pick[0], pick[1], bool(in_basin))
        if not in_basin:
            notes.append("같은 유역에 비 관측소 없음 → 가장 가까운 것")

    water = None
    for s, d in near:
        if s.kind != "waterlevel" or not same(s):
            continue
        n = alive_of(s.code)
        if n == 0:
            notes.append(f"수위계 {s.name} 같은 유역이지만 최근 자료 없음")
            continue
        rv = river_of(s.lat, s.lon)
        water = (s, d, rv[1] if rv else "", n)
        break

    if rain is None or (not rain[2] and rain[1] > NEAR_KM):
        cls = "C"
    elif water:
        cls = "A"
        if vr and water[2] and vr[1] != water[2]:
            notes.append(f"계곡 옆 하천 '{vr[1]}' ≠ 수위계 하천 '{water[2]}' (같은 유역 안 다른 지류일 수 있음)")
    else:
        cls = "B"
    return Verdict(cls, rain, water, vb, vr, notes)


def alive_checker(client, kind="waterlevel"):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from app import replay

    cache = {}
    end = replay.floor10(datetime.now(ZoneInfo("Asia/Seoul")))

    def alive(code):
        if code not in cache:
            try:
                s = replay.fetch(code, kind, end - timedelta(hours=3), end, client=client)
                cache[code] = sum(v is not None for v in s.values())
            except Exception:  # noqa: BLE001
                cache[code] = 0
        return cache[code]
    return alive


def classify_all() -> None:
    from app import valleys as V

    vs = V.load_csv()
    with _client() as c:
        stations = all_stations(c)
        geo, alive, rain_alive = Geo(c), alive_checker(c), alive_checker(c, "rainfall")
        out = []
        for v in vs:
            vd = classify(float(v.lat), float(v.lon), stations, geo.basin, geo.river, alive, rain_alive)
            out.append((v, vd))
    count = {"A": 0, "B": 0, "C": 0}
    lines = []
    for v, vd in out:
        count[vd.cls] += 1
        b = f"{vd.basin[1]}({vd.basin[0]})" if vd.basin else "유역 모름"
        rv = vd.river[1] if vd.river else "-"
        r = vd.rain
        rain_txt = f"{r[0].name} {r[0].source}:{r[0].code} {r[1]:.1f}km{'' if r[2] else ' (다른 유역)'}" if r else "-"
        w = vd.water
        water_txt = f"{w[0].name} hrfco:{w[0].code} {w[1]:.1f}km 하천 {w[2] or '-'} 최근3h {w[3]}칸" if w else "-"
        now = f"{v.rain_source}:{v.rain_code}" + (f" + 수위 {v.water_code}" if v.water_code else "")
        print(f"\n[{vd.cls}] {v.name}  유역 {b}  옆 하천 {rv}")
        print(f"    비: {rain_txt}")
        print(f"    수위: {water_txt}")
        print(f"    지금 연결: {now}")
        for n in vd.notes:
            print(f"    ⚠ {n}")
        if r:
            wc, wn = (w[0].code, w[0].name) if (w and vd.cls == "A") else ("", "")
            lines.append(f"{v.name},{v.region},{v.lat},{v.lon},{r[0].source},{r[0].code},{r[0].name},{r[0].kind},"
                         f"{wc},{wn},{v.status},{v.note}")
    print(f"\nA 비+수위 {count['A']} / B 비만(AI) {count['B']} / C 보류 {count['C']}")
    path = OUT.parent / "valleys_classified.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("name,region,lat,lon,rain_source,rain_code,rain_name,rain_kind,water_code,water_name,status,note\n"
                    + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"분류대로 고친 목록: {path}  (확인 후 db/valleys.csv로)")


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return
    if argv[0] == "--classify":
        classify_all()
        return
    if argv[0] == "--scan":
        scan(argv[1:] or ["원주", "횡성", "영월", "강릉"])
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
