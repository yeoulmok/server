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


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
