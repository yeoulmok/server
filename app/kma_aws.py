"""기상청 API허브 AWS 매분자료(nph-aws2_min)에서 1시간 누적 강수(RN-60m)를 뽑는다.

- stations.kind = 'rainfall_1h' 인 kma 관측소에 쓴다. 값이 이미 1시간 누적이라
  판정 엔진은 더하지 않고 최신 값을 그대로 쓴다.
- 매분 자료 중 10분 정각(…:00, …:10, …)만 저장해 HRFCO 10분 자료와 간격을 맞춘다.
- 문서 기준 -50 이하는 관측 없음/에러 → NULL.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
URL = "https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-aws2_min"
COLUMN = os.environ.get("KMA_AWS_RAIN_COLUMN", "RN-60m")
LOOKBACK = timedelta(hours=1)
MISSING_AT_OR_BELOW = Decimal("-50")


def parse(text: str, column: str = COLUMN) -> list[tuple[datetime, Decimal | None]]:
    """공백 정렬 텍스트에서 (관측시각, column 값)을 10분 정각만 뽑는다."""
    col: int | None = None
    out: list[tuple[datetime, Decimal | None]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            tokens = s.lstrip("#").split()
            if column in tokens and "STN" in tokens:   # 변수명 줄
                col = tokens.index(column)
            continue
        if col is None:
            raise ValueError(f"응답 헤더에서 {column} 열을 찾지 못함")
        p = s.split()
        if len(p) <= col:
            continue
        try:
            t = datetime.strptime(p[0], "%Y%m%d%H%M").replace(tzinfo=KST)
        except ValueError:
            continue
        if t.minute % 10:
            continue
        try:
            v: Decimal | None = Decimal(p[col])
        except InvalidOperation:
            v = None
        if v is not None and v <= MISSING_AT_OR_BELOW:
            v = None
        out.append((t, v))
    return out


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("euc-kr", "replace")


def fetch_kma_aws(client, station, now: datetime) -> list[tuple[datetime, Decimal | None]]:
    end = now.astimezone(KST).replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 10)
    return _get(client, station.code, end - LOOKBACK, end)


RANGE_CHUNK = timedelta(hours=6)     # 매분 자료라 한 번에 너무 길게 받지 않음 (6시간 = 360줄)


def fetch_range(code: str, start: datetime, end: datetime, client=None) -> dict[datetime, Decimal | None]:
    """지난 자료 (backfill·재현용). 6시간씩 나눠 받아 10분 정각만 모은다. 한 조각이 비어도 계속."""
    own = client is None
    if own:
        import httpx

        client = httpx.Client(timeout=30)
    out: dict[datetime, Decimal | None] = {}
    try:
        t = start
        while t < end:
            t2 = min(t + RANGE_CHUNK, end)
            for attempt in (1, 2):           # 기상청이 늦게 답하면 한 번 더 (11/20 ReadTimeout)
                try:
                    out.update(_get(client, code, t, t2))
                    break
                except ValueError:
                    break                    # 그 조각만 자료 없음 → 비워 둠
                except Exception as e:  # noqa: BLE001 — 네트워크 오류(타임아웃 등)
                    if attempt == 2:
                        print(f"  기상청 AWS {code} {t:%m/%d %H:%M}~{t2:%H:%M} 못 받음 ({type(e).__name__}) → 비워 둠")
            t = t2
    finally:
        if own:
            client.close()
    return out


def _get(client, code: str, start: datetime, end: datetime) -> list[tuple[datetime, Decimal | None]]:
    fmt = "%Y%m%d%H%M"
    resp = client.get(URL, params={
        "tm1": start.strftime(fmt),
        "tm2": end.strftime(fmt),
        "stn": code,
        "disp": "0",
        "help": "0",
        "authKey": os.environ["KMA_KEY"],
    })
    text = _decode(resp.content)
    if resp.status_code != 200:
        # raise_for_status()는 키가 든 URL을 메시지에 넣으므로 쓰지 않는다
        raise ValueError(f"HTTP {resp.status_code}: {text[:150]}")
    rows = parse(text)
    if not rows:
        raise ValueError(f"자료 없음: {text[:150]}")
    return rows
