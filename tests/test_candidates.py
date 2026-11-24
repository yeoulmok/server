"""계곡 후보(app/candidates.py) — API 없이 응답 모양만.  실행: python tests/test_candidates.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import candidates as C  # noqa: E402


def test_dms_and_km():
    assert abs(C.dms("37-17-54") - 37.298333) < 1e-5 and C.dms(" 128-08-33 ") > 128.14 and C.dms("") is None
    assert abs(C.dms("37.25") - 37.25) < 1e-9
    assert 9.0 < C.km(37.2787, 127.942, 37.2787, 128.0438) < 9.1          # 경도 0.1도 ≈ 8.9km (위도 37도)


def test_parse_tour_filters_valleys():
    payload = {"response": {"body": {"totalCount": 3, "items": {"item": [
        {"title": "병지방계곡", "addr1": "강원특별자치도 횡성군 갑천면 병지방리", "mapx": "128.05", "mapy": "37.55", "contentid": "1"},
        {"title": "병지방계곡 오토캠핑장", "addr1": "강원특별자치도 횡성군 갑천면", "mapx": "128.06", "mapy": "37.56", "contentid": "2"},
        {"title": "좌표없는계곡", "addr1": "강원특별자치도 원주시", "mapx": "", "mapy": "", "contentid": "3"}]}}}}
    places, total = C.parse_tour(payload)
    assert total == 3 and [p.name for p in places] == ["병지방계곡", "병지방계곡 오토캠핑장"]
    assert [p.name for p in places if C.is_valley(p.name)] == ["병지방계곡"]
    one = {"response": {"body": {"totalCount": 1, "items": {"item": payload["response"]["body"]["items"]["item"][0]}}}}
    assert len(C.parse_tour(one)[0]) == 1                                  # 결과 1개면 dict로 옴
    assert C.parse_tour({"response": {"body": {"totalCount": 0, "items": ""}}}) == ([], 0)
    try:
        C.parse_tour({"response": {"header": {"resultCode": "30", "resultMsg": "SERVICE_KEY_IS_NOT_REGISTERED"}}})
    except ValueError as e:
        assert "SERVICE_KEY" in str(e)
    else:
        raise AssertionError("오류 코드는 0건으로 넘기면 안 됨")


def test_parse_hrfco_and_kma():
    h = C.parse_hrfco({"content": [{"rfobscd": "10024240", "obsnm": "원주시(황둔리)", "lat": "37-15-04", "lon": "128-08-33"},
                                   {"rfobscd": "x", "obsnm": "좌표없음", "lat": "", "lon": ""}]}, "rainfall")
    assert len(h) == 1 and h[0].code == "10024240" and h[0].kind == "rainfall"
    text = "# STN LON LAT STN_SP HT ... STN_KO\n 583 128.1630 37.4790 AWS 400.0 안흥 Anheung\n 561 127.98 37.6 AWS 300 청일 Cheongil\n"
    k = C.parse_kma_stn(text)
    assert [(s.code, s.name, round(s.lat, 3)) for s in k] == [("583", "안흥", 37.479), ("561", "청일", 37.6)]


def test_rows_prefer_hrfco_unless_kma_much_closer():
    p = C.Place("가계곡", "강원특별자치도 원주시 신림면 황둔리", 37.25, 128.14, "tour:9")
    st = [C.Station("hrfco", "H", "황둔리", "rainfall", 37.251, 128.1425),
          C.Station("kma", "582", "신림", "rainfall_1h", 37.27, 128.09),
          C.Station("hrfco", "W", "어딘가교", "waterlevel", 37.3, 128.0)]
    r = C.rows_for([p], st)[0]
    assert r["pick"][0].code == "H" and r["water"][0].code == "W"
    line = C.csv_line(r)
    assert line.startswith("가계곡,원주시 신림면,37.250000,128.140000,hrfco,H,황둔리,rainfall,,,시범,")
    far = C.Place("나계곡", "강원특별자치도 원주시", 37.27, 128.0899, "vworld")
    assert C.rows_for([far], st)[0]["pick"][0].code == "582"                 # 기상청이 절반 이하 거리면 기상청


def test_vworld_parsers():
    s = C.parse_vworld_search({"response": {"result": {"items": [
        {"title": "용수골계곡", "address": {"parcel": "원주시 판부면 서곡리"}, "point": {"x": "127.94", "y": "37.27"}}]}}})
    assert s[0].name == "용수골계곡" and s[0].lat == 37.27
    g = C.parse_vworld_geocode({"response": {"result": {"point": {"x": "127.942", "y": "37.2787"}}}}, "서곡리 산 166")
    assert g.lon == 127.942 and C.parse_vworld_geocode({"response": {"status": "NOT_FOUND"}}, "x") is None


# --- 분류 (#52) ---------------------------------------------------------------

def _geo(basins, rivers=None):
    rivers = rivers or {}
    return (lambda la, lo: basins.get((round(la, 3), round(lo, 3)))), (lambda la, lo: rivers.get((round(la, 3), round(lo, 3))))


def test_classify_a_when_same_basin_gauge_alive():
    V = (37.278, 128.020)                                       # 금대리
    st = [C.Station("kma", "582", "신림", "rainfall_1h", 37.270, 128.090),
          C.Station("hrfco", "R", "신촌리", "rainfall", 37.297, 127.971),
          C.Station("hrfco", "1006664", "동부교", "waterlevel", 37.319, 127.970),
          C.Station("hrfco", "X", "딴유역교", "waterlevel", 37.280, 128.030)]
    basin, river = _geo({(37.278, 128.02): ("100606", "원주천"), (37.27, 128.09): ("100607", "다른"),
                         (37.297, 127.971): ("100606", "원주천"), (37.319, 127.97): ("100606", "원주천"),
                         (37.28, 128.03): ("100999", "딴데")},
                        {(37.319, 127.97): ("지방2급하천", "원주천")})
    vd = C.classify(*V, st, basin, river, lambda code: 18)
    assert vd.cls == "A" and vd.water[0].code == "1006664" and vd.water[2] == "원주천"
    assert vd.rain[0].code == "R" and vd.rain[2] is True          # 같은 유역 HRFCO 우선 (신림은 다른 유역)


def test_classify_b_when_gauge_dead_or_other_basin():
    V = (37.278, 128.020)
    st = [C.Station("hrfco", "R", "비", "rainfall", 37.279, 128.021),
          C.Station("hrfco", "W", "죽은교", "waterlevel", 37.280, 128.022)]
    basin, river = _geo({(37.278, 128.02): ("1", "a"), (37.279, 128.021): ("1", "a"), (37.28, 128.022): ("1", "a")})
    vd = C.classify(*V, st, basin, river, lambda code: 0)
    assert vd.cls == "B" and vd.water is None and any("최근 자료 없음" in n for n in vd.notes)


def test_classify_c_when_rain_far_and_other_basin():
    V = (37.0, 128.0)
    st = [C.Station("hrfco", "R", "먼비", "rainfall", 37.09, 128.0)]             # 10km, 다른 유역
    basin, river = _geo({(37.0, 128.0): ("1", "a"), (37.09, 128.0): ("2", "b")})
    assert C.classify(*V, st, basin, river, lambda c: 0).cls == "C"


def test_classify_skips_dead_rain_station():
    V = (37.278, 128.020)
    st = [C.Station("hrfco", "DEAD", "신촌리", "rainfall", 37.297, 127.971),
          C.Station("kma", "582", "신림", "rainfall_1h", 37.270, 128.090)]
    basin, river = _geo({(37.278, 128.02): ("100606", "원주천"), (37.297, 127.971): ("100606", "원주천"),
                         (37.27, 128.09): ("100607", "다른")})
    vd = C.classify(*V, st, basin, river, lambda c: 0, lambda c: 0 if c == "DEAD" else 18)
    assert vd.rain[0].code == "582" and any("신촌리 최근 자료 없음" in n for n in vd.notes)


def test_vworld_feature_parser():
    ok = {"response": {"status": "OK", "result": {"featureCollection": {"features": [
        {"properties": {"sbsncd": "100606", "sbsnnm": "원주천", "cat_nam": "표준유역"}}]}}}}
    assert C.parse_vworld_feature(ok, "sbsncd", "sbsnnm") == ("100606", "원주천")
    assert C.parse_vworld_feature({"response": {"status": "NOT_FOUND"}}, "sbsncd", "sbsnnm") is None


def test_link_changes():
    from app import valleys as VV
    v = next(x for x in VV.load_csv() if x.name == "금대리 계곡")
    same = {"upstream_rain": ("kma", "582", "rainfall_1h"), "downstream_water": ("hrfco", "1006664", "waterlevel")}
    assert VV.link_changes(VV.wanted_links(v), same) == []
    g = next(x for x in VV.load_csv() if x.name == "구룡사계곡")
    import dataclasses
    add_water = dataclasses.replace(g, water_code="1006667", water_name="섬강교")
    ch = VV.link_changes(VV.wanted_links(add_water), {"upstream_rain": ("kma", "591", "rainfall_1h")})
    assert ch == [("downstream_water", None, ("hrfco", "1006667", "waterlevel", "섬강교"))]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
