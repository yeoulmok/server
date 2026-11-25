"""행정안전부_물놀이_관리지역 (재난안전데이터 공유플랫폼 DSSP-IF-00186, #63).

  python -u -m app.safemap --probe      # 응답 모양만 보기 (키는 안 찍음)
키: ~/secrets/yeoulmok.env 의 SAFEMAP_KEY
"""
from __future__ import annotations

import json
import os
import sys

URL = "https://www.safetydata.go.kr/V2/api/DSSP-IF-00186"


def shape(x, depth=0, max_depth=4) -> list[str]:
    """JSON 구조만 (값은 짧게). 첫 항목만 펼침."""
    pad = "  " * depth
    out = []
    if isinstance(x, dict):
        for k, v in x.items():
            if isinstance(v, (dict, list)) and depth < max_depth:
                out.append(f"{pad}{k}: {type(v).__name__}{f'[{len(v)}]' if isinstance(v, list) else ''}")
                out += shape(v, depth + 1, max_depth)
            else:
                out.append(f"{pad}{k}: {str(v)[:60]!r}")
    elif isinstance(x, list) and x:
        out += shape(x[0], depth, max_depth)
    return out


def probe() -> None:
    import httpx

    key = os.environ.get("SAFEMAP_KEY")
    if not key:
        sys.exit("SAFEMAP_KEY 없음 → env 넣고 judge 다시 만들기")
    tries = [
        {"pageNo": 1, "numOfRows": 3, "returnType": "json"},
        {"pageNo": 1, "numOfRows": 3},
    ]
    with httpx.Client(timeout=30) as c:
        for extra in tries:
            r = c.get(URL, params={"serviceKey": key, **extra})
            print(f"\n== 파라미터 {list(extra)} → HTTP {r.status_code}, {r.headers.get('content-type')}")
            body = r.text.replace(key, "***")
            try:
                data = json.loads(body)
            except ValueError:
                print(body[:600])
                continue
            print("\n".join(shape(data)))
            break


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    else:
        print(__doc__)
