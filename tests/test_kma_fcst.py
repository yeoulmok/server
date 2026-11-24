import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import kma_fcst as F  # noqa: E402


def test_to_grid_known_points():
    assert F.to_grid(37.5665, 126.9780) == (60, 127)       # 서울 시청
    assert F.to_grid(37.3422, 127.9202) == (76, 122)       # 원주


def test_base_time():
    assert F.base_time(datetime(2026, 11, 24, 13, 50)) == ("20261124", "1330")
    assert F.base_time(datetime(2026, 11, 24, 13, 40)) == ("20261124", "1230")
    assert F.base_time(datetime(2026, 11, 24, 0, 10)) == ("20261123", "2330")


def test_parse():
    p = {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": [
        {"category": "T1H", "fcstTime": "1400", "fcstValue": "5"},
        {"category": "RN1", "fcstTime": "1500", "fcstValue": "1.0mm"},
        {"category": "RN1", "fcstTime": "1400", "fcstValue": "강수없음"}]}}}}
    assert F.parse(p) == [("1400", "강수없음"), ("1500", "1.0mm")]


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
