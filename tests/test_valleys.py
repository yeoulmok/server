"""계곡 목록(app/valleys.py)·기상청 AWS 지난 자료·재현 kma 테스트 — DB·API 없이.  실행: python tests/test_valleys.py"""

import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import kma_aws  # noqa: E402
from app import reconstruct as R  # noqa: E402
from app import valleys as V  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def t(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def test_csv_loads():
    vs = V.load_csv()
    names = [v.name for v in vs]
    assert names[:2] == ["금대리 계곡", "서곡리 용수골 계곡"], names
    assert "구룡사계곡" in names
    g = next(v for v in vs if v.name == "구룡사계곡")
    assert (g.rain_source, g.rain_code, g.rain_kind, g.water_code, g.status) == ("kma", "591", "rainfall_1h", "", "시범")


def test_csv_rejects_bad(tmp=Path("/tmp/valleys_bad.csv")):
    head = "name,region,lat,lon,rain_source,rain_code,rain_name,rain_kind,water_code,water_name,status,note\n"
    for row, msg in (("A,원주,37.3,128.0,kma,591,x,rainfall,,,,\n", "종류"),
                     ("A,원주,127.9,37.3,kma,591,x,rainfall_1h,,,,\n", "한국 밖"),
                     ("A,원주,37.3,128.0,kma,591,x,rainfall_1h,,,,\nA,원주,37.3,128.0,kma,591,x,rainfall_1h,,,,\n", "중복")):
        tmp.write_text(head + row, encoding="utf-8")
        try:
            V.load_csv(tmp)
        except ValueError as e:
            assert msg in str(e), e
        else:
            raise AssertionError(f"막아야 함: {msg}")


def test_missing_only_new():
    vs = V.load_csv()
    todo = V.missing(vs, {"금대리 계곡", "서곡리 용수골 계곡"})
    assert [v.name for v in todo] == [v.name for v in vs[2:]]


class FakeCur:
    def __init__(self):
        self.sql, self.rowcount, self._next = [], 3, None
        self._ids = iter(range(100, 200))

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))
        self._next = (next(self._ids),) if ("RETURNING id" in sql or sql.startswith("SELECT id")) else None

    def fetchone(self):
        return self._next


def test_add_valley_rain_only():
    v = next(x for x in V.load_csv() if x.name == "구룡사계곡")
    cur = FakeCur()
    vid = V.add_valley(cur, v)
    text = [s for s, _ in cur.sql]
    assert vid == 100
    assert sum(s.startswith("INSERT INTO stations") for s in text) == 1          # 강수만 (수위 없음)
    links = [p for s, p in cur.sql if s.startswith("INSERT INTO valley_stations")]
    assert [p[2] for p in links] == ["upstream_rain"]
    assert any(s.startswith("INSERT INTO valley_thresholds") and p[1] == "금대리 계곡" for s, p in cur.sql)


def test_add_valley_with_water():
    v = next(x for x in V.load_csv() if x.name == "금대리 계곡")
    cur = FakeCur()
    V.add_valley(cur, v)
    roles = [p[2] for s, p in cur.sql if s.startswith("INSERT INTO valley_stations")]
    assert roles == ["upstream_rain", "downstream_water"]


class FakeResp:
    def __init__(self, text, status=200):
        self.content, self.status_code = text.encode(), status


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, url, params):
        self.calls.append((params["tm1"], params["tm2"]))
        start = datetime.strptime(params["tm1"], "%Y%m%d%H%M")
        if start.hour == 6:                      # 06~12시 조각은 자료 없음
            return FakeResp("# 자료 없음")
        lines = ["# YYMMDDHHMI STN RN-60m"]
        x = start
        while x <= datetime.strptime(params["tm2"], "%Y%m%d%H%M"):
            lines.append(f"{x:%Y%m%d%H%M} 591 {x.hour}.0")
            x += timedelta(minutes=1)
        return FakeResp("\n".join(lines))


def test_kma_fetch_range_chunks_and_skips_empty():
    os.environ.setdefault("KMA_KEY", "test")
    c = FakeClient()
    s = kma_aws.fetch_range("591", t("2025-07-19 00:00"), t("2025-07-19 18:00"), client=c)
    assert c.calls == [("202507190000", "202507190600"), ("202507190600", "202507191200"),
                       ("202507191200", "202507191800")]
    assert all(k.minute % 10 == 0 for k in s)
    assert t("2025-07-19 03:10") in s and t("2025-07-19 09:00") not in s       # 빈 조각은 건너뜀
    assert s[t("2025-07-19 13:00")] == D("13.0")


class FlakyClient(FakeClient):
    def __init__(self, fail_times):
        super().__init__()
        self.fail_times = fail_times

    def get(self, url, params):
        if self.fail_times:
            self.fail_times -= 1
            self.calls.append(("timeout", ""))
            raise TimeoutError("read timed out")
        return super().get(url, params)


def test_kma_fetch_range_retries_then_skips():
    os.environ.setdefault("KMA_KEY", "test")
    c = FlakyClient(fail_times=1)                     # 첫 시도만 늦음 → 다시 받아서 성공
    s = kma_aws.fetch_range("591", t("2025-07-19 00:00"), t("2025-07-19 06:00"), client=c)
    assert c.calls[0] == ("timeout", "") and t("2025-07-19 03:00") in s
    c = FlakyClient(fail_times=2)                     # 두 번 다 늦음 → 그 조각만 비우고 다음 조각 계속
    s = kma_aws.fetch_range("591", t("2025-07-19 00:00"), t("2025-07-19 18:00"), client=c)
    assert t("2025-07-19 03:00") not in s and t("2025-07-19 13:00") in s


def test_reconstruct_kma_steps_every_10min():
    s = {}
    x = t("2025-07-16 00:00")
    while x <= t("2025-07-19 06:00"):
        s[x] = D("20") if x >= t("2025-07-19 03:00") else D("0")   # 1시간 누적 20mm → 규칙 주의
        x += timedelta(minutes=10)
    kind, series = R.load_kma("591", t("2025-07-16 00:00"), t("2025-07-19 06:00"), fetch=lambda c, a, b: s)
    rows = R.run(series, kind, t("2025-07-19 02:00"), t("2025-07-19 04:00"), R.DEFAULT_THRESHOLDS, None)
    assert len(rows) == 13                                          # 10분 간격
    assert R.first_at(rows, "caution") == t("2025-07-19 03:00")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
