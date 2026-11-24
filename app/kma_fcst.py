"""기상청 단기예보 조회서비스 — 초단기예보 시험 호출 (#67, 판정에는 아직 안 씀).

  python -m app.kma_fcst 서곡리         # 계곡 이름 일부 → 그 자리 격자의 앞으로 6시간 강수 예보
키: 공공데이터포털 키 (TOUR_KEY와 같은 키, 활용신청만 하면 됨)
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtFcst"


def to_grid(lat: float, lon: float) -> tuple[int, int]:
    """위경도 → 기상청 5km 격자 (기상청 공개 변환식, LCC)."""
    re, grid, slat1, slat2, olon, olat, xo, yo = 6371.00877, 5.0, 30.0, 60.0, 126.0, 38.0, 43, 136
    d = math.pi / 180.0
    r = re / grid
    s1, s2, ol, oa = slat1 * d, slat2 * d, olon * d, olat * d
    sn = math.log(math.cos(s1) / math.cos(s2)) / math.log(math.tan(math.pi / 4 + s2 / 2) / math.tan(math.pi / 4 + s1 / 2))
    sf = math.tan(math.pi / 4 + s1 / 2) ** sn * math.cos(s1) / sn
    ro = r * sf / math.tan(math.pi / 4 + oa / 2) ** sn
    ra = r * sf / math.tan(math.pi / 4 + lat * d / 2) ** sn
    th = lon * d - ol
    th = (th + 2 * math.pi) if th < -math.pi else (th - 2 * math.pi) if th > math.pi else th
    th *= sn
    return int(ra * math.sin(th) + xo + 0.5), int(ro - ra * math.cos(th) + yo + 0.5)


def base_time(now: datetime) -> tuple[str, str]:
    """초단기예보는 매시 30분 기준, 45분쯤 나옴 → 45분 전이면 한 시간 전 것."""
    t = now - timedelta(minutes=45)
    return t.strftime("%Y%m%d"), t.strftime("%H") + "30"


def parse(payload: dict) -> list[tuple[str, str]]:
    """→ [(예보 시각 HHMM, RN1 값 문자열)]"""
    head = (payload.get("response") or {}).get("header") or {}
    if head.get("resultCode") not in (None, "00"):
        raise RuntimeError(f"기상청 {head.get('resultCode')}: {head.get('resultMsg')}")
    items = (((payload.get("response") or {}).get("body") or {}).get("items") or {}).get("item") or []
    return sorted((i["fcstTime"], i["fcstValue"]) for i in items if i.get("category") == "RN1")


def main(argv=None) -> None:
    import httpx

    from app import valleys as V

    argv = sys.argv[1:] if argv is None else argv
    want = argv[0] if argv else "서곡리"
    vs = [v for v in V.load_csv() if want in v.name]
    if not vs:
        sys.exit(f"'{want}' 계곡 없음")
    d, t = base_time(datetime.now(KST))
    with httpx.Client(timeout=20) as c:
        for v in vs:
            nx, ny = to_grid(float(v.lat), float(v.lon))
            r = c.get(URL, params={"serviceKey": os.environ["TOUR_KEY"], "pageNo": 1, "numOfRows": 60, "dataType": "JSON",
                                   "base_date": d, "base_time": t, "nx": nx, "ny": ny})
            if r.status_code != 200 or not r.text.lstrip().startswith("{"):
                sys.exit(f"응답 {r.status_code}: {r.text[:200]}  (활용신청 승인 전이면 1~2시간 뒤 다시)")
            rows = parse(r.json())
            print(f"■ {v.name}  격자 {nx},{ny}  (기준 {d} {t})")
            for ft, val in rows:
                print(f"  {ft[:2]}:{ft[2:]}  1시간 강수 {val}")


if __name__ == "__main__":
    main()
