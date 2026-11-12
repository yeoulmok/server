"""관측 데이터 수집기.

stations 테이블을 읽어 source/kind별로 외부 API를 호출하고,
최근 1시간치 관측값을 observations 테이블에 upsert한다.

- run_once(): 1회 수집 (systemd timer에서 호출할 진입점)
- main():     5분 간격 반복 루프

실행:  python -m app.collector          # 루프
       python -m app.collector --once   # 1회
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import httpx
import psycopg

from app.kma_aws import fetch_kma_aws

log = logging.getLogger("collector")

KST = ZoneInfo("Asia/Seoul")

INTERVAL_SECONDS = 5 * 60
LOOKBACK = timedelta(hours=1)
MAX_RETRIES = 3          # 첫 시도 실패 후 재시도 횟수 (총 시도 = 1 + 3)
RETRY_DELAY_SECONDS = 5
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

HRFCO_BASE = "http://api.hrfco.go.kr"
KMA_BASE = "https://apihub.kma.go.kr/api/typ01/url"
KMA_HOURLY_PATH = "kma_sfctm3.php"          # 지상관측 시간자료(기간)
KMA_RAIN_COLUMN = os.environ.get("KMA_RAIN_COLUMN", "RN_1HR")
KMA_MISSING = {Decimal("-99.9")}

# HRFCO 응답에서 kind별 값 필드
HRFCO_VALUE_FIELD = {"waterlevel": "wl", "rainfall": "rf"}


@dataclass(frozen=True)
class Station:
    id: int
    source: str
    code: str
    kind: str


# 수집 결과 한 건: (관측시각, 값 또는 None)
Obs = tuple[datetime, Decimal | None]


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------

def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"환경변수 {name}가 비어 있음")
    return value


def _to_decimal(raw: object) -> Decimal | None:
    """빈 값·공백·'-' 등은 None. 0으로 바꾸지 않는다."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text in ("", "-", "--", "null", "None"):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _floor_minutes(dt: datetime, step: int) -> datetime:
    return dt.replace(minute=dt.minute - dt.minute % step, second=0, microsecond=0)


def with_retry(fn: Callable[[], list[Obs]], label: str) -> list[Obs]:
    """fn을 실행하고 실패하면 RETRY_DELAY_SECONDS 간격으로 MAX_RETRIES번 재시도."""
    last_exc: Exception | None = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            return fn()
        except Exception as exc:  # 네트워크·파싱 오류 모두 재시도 대상
            last_exc = exc
            if attempt < MAX_RETRIES:
                log.warning("%s 실패 (%d/%d): %s — %ds 후 재시도",
                            label, attempt + 1, 1 + MAX_RETRIES, exc, RETRY_DELAY_SECONDS)
                time.sleep(RETRY_DELAY_SECONDS)
    raise RuntimeError(f"{label}: {1 + MAX_RETRIES}회 시도 모두 실패") from last_exc


# ---------------------------------------------------------------------------
# 한강홍수통제소 (HRFCO)
# ---------------------------------------------------------------------------

def fetch_hrfco(client: httpx.Client, station: Station, now: datetime) -> list[Obs]:
    end = _floor_minutes(now, 10)
    start = end - LOOKBACK
    fmt = "%Y%m%d%H%M"  # 10M 간격은 12자리
    url = (f"{HRFCO_BASE}/{_env('HRFCO_KEY')}/{station.kind}/list/10M/"
           f"{station.code}/{start.strftime(fmt)}/{end.strftime(fmt)}.json")

    resp = client.get(url)
    resp.raise_for_status()
    payload = resp.json()

    rows = payload.get("content")
    if rows is None:
        # 키 오류 등은 content 없이 메시지만 오는 경우가 있음
        raise ValueError(f"응답에 content 없음: {str(payload)[:200]}")

    field = HRFCO_VALUE_FIELD[station.kind]
    result: list[Obs] = []
    for row in rows:
        ymdhm = str(row.get("ymdhm", "")).strip()
        if len(ymdhm) != 12:
            continue
        observed_at = datetime.strptime(ymdhm, fmt).replace(tzinfo=KST)
        result.append((observed_at, _to_decimal(row.get(field))))
    return result


# ---------------------------------------------------------------------------
# 기상청 API허브 지상관측 시간자료
# ---------------------------------------------------------------------------

def _parse_kma_text(text: str, column: str) -> list[tuple[str, str]]:
    """공백 구분 텍스트에서 (관측시각 문자열, column 값) 목록을 뽑는다.

    '#'로 시작하는 주석 줄 중 column 이름이 들어 있는 줄을 헤더로 보고
    열 위치를 찾는다. 열 순서를 하드코딩하지 않기 위함.
    """
    col_idx: int | None = None
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            tokens = stripped.lstrip("#").split()
            if column in tokens:
                col_idx = tokens.index(column)
            continue
        if col_idx is None:
            raise ValueError(f"응답 헤더에서 {column} 열을 찾지 못함")
        parts = stripped.split()
        if len(parts) <= col_idx:
            continue
        out.append((parts[0], parts[col_idx]))
    return out


def fetch_kma(client: httpx.Client, station: Station, now: datetime) -> list[Obs]:
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - LOOKBACK
    fmt = "%Y%m%d%H%M"
    params = {
        "tm1": start.strftime(fmt),
        "tm2": end.strftime(fmt),
        "stn": station.code,
        "help": "1",   # 열 이름 헤더를 받기 위해 1
        "authKey": _env("KMA_KEY"),
    }
    resp = client.get(f"{KMA_BASE}/{KMA_HOURLY_PATH}", params=params)
    resp.raise_for_status()
    text = resp.content.decode("euc-kr", errors="replace")

    result: list[Obs] = []
    for tm, raw in _parse_kma_text(text, KMA_RAIN_COLUMN):
        try:
            observed_at = datetime.strptime(tm[:12], fmt).replace(tzinfo=KST)
        except ValueError:
            continue
        value = _to_decimal(raw)
        if value is not None and (value in KMA_MISSING or value < 0):
            value = None  # 결측(-99.9 등). 강수량은 음수가 나올 수 없음
        result.append((observed_at, value))
    return result


FETCHERS: dict[tuple[str, str], Callable[[httpx.Client, Station, datetime], list[Obs]]] = {
    ("hrfco", "waterlevel"): fetch_hrfco,
    ("hrfco", "rainfall"): fetch_hrfco,
    ("kma", "rainfall"): fetch_kma,
    ("kma", "rainfall_1h"): fetch_kma_aws,
}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def connect() -> psycopg.Connection:
    return psycopg.connect(
        host=_env("DB_HOST"),
        user=_env("POSTGRES_USER"),
        password=_env("POSTGRES_PASSWORD"),
        dbname=_env("POSTGRES_DB"),
        connect_timeout=10,
        # 관측소별 save()가 각자 커밋되도록. 중간에 죽어도 앞서 저장한 건 남는다.
        autocommit=True,
    )


def load_stations(conn: psycopg.Connection) -> list[Station]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, source, code, kind FROM stations ORDER BY source, kind, id")
        return [Station(*row) for row in cur.fetchall()]


UPSERT_SQL = """
INSERT INTO observations (station_id, observed_at, value, collected_at)
VALUES (%s, %s, %s, now())
ON CONFLICT (station_id, observed_at)
DO UPDATE SET value = COALESCE(EXCLUDED.value, observations.value), collected_at = EXCLUDED.collected_at
"""


def save(conn: psycopg.Connection, station: Station, rows: Iterable[Obs]) -> int:
    params = [(station.id, observed_at, value) for observed_at, value in rows]
    if not params:
        return 0
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, params)
    return len(params)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def _group(stations: list[Station]) -> dict[tuple[str, str], list[Station]]:
    groups: dict[tuple[str, str], list[Station]] = {}
    for st in stations:
        groups.setdefault((st.source, st.kind), []).append(st)
    return groups


def run_once() -> dict[str, int]:
    """1회 수집. 관측소 하나가 실패해도 나머지는 계속 진행한다."""
    now = datetime.now(KST)
    stats = {"ok": 0, "failed": 0, "skipped": 0, "rows": 0}

    with connect() as conn, httpx.Client(timeout=HTTP_TIMEOUT) as client:
        stations = load_stations(conn)
        for (source, kind), group in _group(stations).items():
            fetcher = FETCHERS.get((source, kind))
            if fetcher is None:
                log.warning("지원하지 않는 조합 source=%s kind=%s (%d곳) 건너뜀",
                            source, kind, len(group))
                stats["skipped"] += len(group)
                continue

            log.info("%s/%s 관측소 %d곳 수집 시작", source, kind, len(group))
            for st in group:
                label = f"{source}/{kind} {st.code}(id={st.id})"
                try:
                    rows = with_retry(lambda: fetcher(client, st, now), label)
                    n = save(conn, st, rows)
                    stats["ok"] += 1
                    stats["rows"] += n
                    log.debug("%s: %d건 저장", label, n)
                except Exception:
                    stats["failed"] += 1
                    log.exception("%s: 수집 실패, 다음 관측소로 넘어감", label)

    log.info("수집 완료: 성공 %(ok)d, 실패 %(failed)d, 건너뜀 %(skipped)d, 저장 %(rows)d건", stats)
    return stats


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if "--once" in sys.argv:
        stats = run_once()
        sys.exit(1 if stats["ok"] == 0 and stats["failed"] > 0 else 0)

    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception:
            # DB 접속 실패 등 회차 전체 오류도 루프는 유지
            log.exception("수집 회차 전체 실패")
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
