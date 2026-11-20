"""계곡 목록 파일(db/valleys.csv) → DB (#42). 계곡 추가 = 목록에 한 줄 + 명령 한 번.

  python -m app.valleys            # 목록과 DB 상태 보기 (아무것도 안 바꿈)
  python -m app.valleys apply      # DB에 없는 계곡만 추가 + 새 강수 관측소 지난 73시간 자료 받기

- 이미 DB에 있는 계곡(이름 기준)은 건드리지 않음. 여러 번 실행해도 같은 결과
- 하류 수위가 없는 계곡은 강수만 연결 → 판정 엔진이 AI 확인 단계를 씀 (서곡리 방식)
- 기준값은 금대리 값을 복사 (1시간 강수 15/25/40mm, 임시값 #20)
- status: 운영(검증함) / 시범(재현 전). 보고서에 "검증 N곳 / 시범 M곳"으로 쓰려고 적어 둠
"""

from __future__ import annotations

import csv
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("valleys")

KST = ZoneInfo("Asia/Seoul")
CSV_PATH = Path(__file__).resolve().parents[1] / "db" / "valleys.csv"
BASE_VALLEY = "금대리 계곡"          # 기준값을 복사해 올 계곡
BACKFILL_HOURS = 73                 # AI가 72시간 강수를 봄
RAIN_KINDS = {("hrfco", "rainfall"), ("kma", "rainfall_1h")}


@dataclass(frozen=True)
class Valley:
    name: str
    region: str
    lat: Decimal
    lon: Decimal
    rain_source: str
    rain_code: str
    rain_name: str
    rain_kind: str
    water_code: str
    water_name: str
    status: str
    note: str


def load_csv(path: Path = CSV_PATH) -> list[Valley]:
    out = []
    with path.open(encoding="utf-8-sig") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            r = {k: (v or "").strip() for k, v in r.items()}
            if not r["name"]:
                continue
            if (r["rain_source"], r["rain_kind"]) not in RAIN_KINDS:
                raise ValueError(f"{path.name} {i}줄 '{r['name']}': 강수 관측소 종류 {r['rain_source']}/{r['rain_kind']} 모름")
            lat, lon = Decimal(r["lat"]), Decimal(r["lon"])
            if not (33 < lat < 39 and 124 < lon < 132):
                raise ValueError(f"{path.name} {i}줄 '{r['name']}': 좌표가 한국 밖 ({lat}, {lon})")
            out.append(Valley(r["name"], r["region"], lat, lon, r["rain_source"], r["rain_code"], r["rain_name"],
                              r["rain_kind"], r["water_code"], r["water_name"], r["status"] or "시범", r["note"]))
    names = [v.name for v in out]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ValueError(f"계곡 이름 중복: {', '.join(sorted(dup))}")
    return out


def missing(valleys: list[Valley], existing: set[str]) -> list[Valley]:
    return [v for v in valleys if v.name not in existing]


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _station_id(cur, source: str, code: str, name: str, kind: str) -> int:
    cur.execute("INSERT INTO stations (source, code, name, kind) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (source, code, kind) DO NOTHING", (source, code, name, kind))
    cur.execute("SELECT id FROM stations WHERE source = %s AND code = %s AND kind = %s", (source, code, kind))
    return cur.fetchone()[0]


def add_valley(cur, v: Valley) -> int:
    """계곡 하나 추가 (관측소·연결·기준값). valley id를 돌려줌."""
    cur.execute("INSERT INTO valleys (name, region, lat, lon) VALUES (%s, %s, %s, %s) RETURNING id",
                (v.name, v.region, v.lat, v.lon))
    vid = cur.fetchone()[0]
    links = [(_station_id(cur, v.rain_source, v.rain_code, v.rain_name, v.rain_kind), "upstream_rain")]
    if v.water_code:
        links.append((_station_id(cur, "hrfco", v.water_code, v.water_name or v.water_code, "waterlevel"),
                      "downstream_water"))
    for sid, role in links:
        cur.execute("INSERT INTO valley_stations (valley_id, station_id, role) VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING", (vid, sid, role))
    cur.execute("INSERT INTO valley_thresholds (valley_id, level, rain_1h_mm, water_rise_m) "
                "SELECT %s, level, rain_1h_mm, water_rise_m FROM valley_thresholds "
                "WHERE valley_id = (SELECT id FROM valleys WHERE name = %s) ON CONFLICT DO NOTHING",
                (vid, BASE_VALLEY))
    if cur.rowcount == 0:
        raise RuntimeError(f"'{BASE_VALLEY}' 기준값이 없어서 복사 못 함")
    return vid


def backfill(conn, v: Valley) -> int:
    """새 강수 관측소의 지난 73시간. HRFCO는 collector 것, 기상청 AWS는 kma_aws.fetch_range."""
    from app import collector

    if v.rain_source == "hrfco":
        return collector.backfill(v.rain_code, BACKFILL_HOURS)
    from app.kma_aws import fetch_range

    end = datetime.now(KST).replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 10)
    rows = fetch_range(v.rain_code, end - timedelta(hours=BACKFILL_HOURS), end)
    st = next(s for s in collector.load_stations(conn)
              if (s.source, s.code, s.kind) == (v.rain_source, v.rain_code, v.rain_kind))
    return collector.save(conn, st, sorted(rows.items()))


def existing_names(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM valleys")
        return {r[0] for r in cur.fetchall()}


def show(valleys: list[Valley], existing: set[str]) -> None:
    for v in valleys:
        water = f"수위 {v.water_name or v.water_code}" if v.water_code else "수위 없음 → AI"
        state = "DB에 있음" if v.name in existing else "추가 예정"
        print(f"  [{v.status}] {v.name} ({v.region}) — 강수 {v.rain_name} {v.rain_source}:{v.rain_code}, {water}  … {state}")
    extra = existing - {v.name for v in valleys}
    if extra:
        print(f"  (목록에 없고 DB에만 있음: {', '.join(sorted(extra))})")


def main(argv=None) -> None:
    from app import judge

    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    argv = sys.argv[1:] if argv is None else argv
    valleys = load_csv()
    with judge.connect() as conn:
        existing = existing_names(conn)
        print(f"{CSV_PATH.name}: {len(valleys)}곳")
        show(valleys, existing)
        todo = missing(valleys, existing)
        if not argv or argv[0] != "apply":
            print(f"\n추가할 계곡 {len(todo)}곳. 넣으려면: python -m app.valleys apply")
            return
        for v in todo:
            with conn.transaction(), conn.cursor() as cur:
                vid = add_valley(cur, v)
            print(f"추가: {v.name} (id {vid})")
            try:
                n = backfill(conn, v)
                print(f"  지난 {BACKFILL_HOURS}시간 자료 {n}칸 저장")
            except Exception as e:  # noqa: BLE001 — 계곡은 들어감. 자료는 collector가 앞으로 채움
                print(f"  지난 자료 받기 실패 ({type(e).__name__}: {e}) → 앞으로 수집되는 자료로 판정")
        if not todo:
            print("\n새로 넣을 계곡 없음")
        else:
            print("\njudge가 다음 회차(5분 안)에 새 계곡을 판정함. 확인: docker logs judge --tail 30")


if __name__ == "__main__":
    main()
