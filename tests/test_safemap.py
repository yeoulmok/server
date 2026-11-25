import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import safemap as S  # noqa: E402


def test_to_wgs84():
    lat, lon = S.to_wgs84(14323786.3225, 4306147.53637)          # 응답 예시: 군위군 부계면 동산리
    assert abs(lat - 36.08) < 0.05 and abs(lon - 128.67) < 0.05


def test_items_and_pick():
    rows, total = S.items({"header": {"resultCode": "00"}, "totalCount": "3", "body": [
        {"CTPV_NM": "강원특별자치도", "SGG_NM": "원주시"}, {"CTPV_NM": "강원특별자치도", "SGG_NM": "춘천시"},
        {"CTPV_NM": "대구광역시", "SGG_NM": "군위군"}]})
    assert total == 3 and [r["SGG_NM"] for r in S.pick(rows)] == ["원주시"]
    try:
        S.items({"header": {"resultCode": "30", "resultMsg": "키"}})
        raise AssertionError
    except RuntimeError:
        pass


def test_ri_of():
    assert S.ri_of("강원특별자치도 영월군 김삿갓면 대야리 211-3") == "대야리"
    assert S.ri_of("강원특별자치도 원주시 지정면 소금산길 100") == "지정면"


def test_cluster():
    from app import candidates as C
    x, y = 14250000.0, 4490000.0
    rows = [{"XMAP_CRTS": x, "YMAP_CRTS": y, "SGG_NM": "원주시", "EMY_NM": "지정면", "ADDR": "a", "STOT": "5",
             "WOWA_ACDNT_STOT": "1", "RSK_ZONE_SCTN": "-"},
            {"XMAP_CRTS": x + 500, "YMAP_CRTS": y, "SGG_NM": "원주시", "EMY_NM": "지정면", "ADDR": "b", "STOT": "20",
             "WOWA_ACDNT_STOT": "2", "RSK_ZONE_SCTN": "200", "SFTY_ACTN_MTTR": "통제"},
            {"XMAP_CRTS": x + 9000, "YMAP_CRTS": y, "SGG_NM": "원주시", "EMY_NM": "x", "ADDR": "c"}]
    sp = S.cluster(rows, C.km)
    assert len(sp) == 2 and sp[0].visitors == 20 and sp[0].accidents == 3 and sp[0].risk and sp[0].addrs == ["a", "b"]


def test_pick_all_gangwon():
    rows = [{"CTPV_NM": "강원특별자치도", "SGG_NM": "춘천시"}, {"CTPV_NM": "경기도", "SGG_NM": "가평군"}]
    assert len(S.pick(rows, None)) == 1 and S.pick(rows) == []


def test_dedupe():
    from app import candidates as C
    from types import SimpleNamespace as NS

    def cand(name, river, lat, water=None, rain="R1", risk=False, acc=0, near=3.0):
        sp = S.Spot(lat, 128.0, "춘천시", "x", risk=risk, accidents=acc)
        return S.Cand(name, "춘천시", "x", lat, 128.0, "A" if water else "B", river,
                      ("hrfco", rain, "비", "rainfall", near), (water, "교", near) if water else None, sp)
    existing = [NS(name="있던곳", lat="37.0", lon="128.0", water_code="W9", rain_code="R9")]
    cs = [cand("가", "가천", 37.5, water="W1"), cand("나", "가천", 37.51, water="W1", risk=True),   # 같은 강·수위계 → 위험구역 쪽
          cand("다", "다천", 37.6), cand("라", "다천", 37.61, near=1.0),                         # B 같은 강·비 → 가까운 쪽
          cand("마", "마천", 37.01, water="W9")]                                               # 있던 곳과 같은 수위계 2km
    keep, dropped = S.dedupe(cs, existing, C.km)
    assert sorted(k.name for k in keep) == ["나", "라"] and len(dropped) == 3


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
