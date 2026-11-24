"""AI 설명 (생성형 AI, Claude API) — 경보가 왜 떴는지 쉬운 문장으로.

신청서 약속: 규칙 엔진이 발령하고 AI는 근거만 설명한다. AI가 새 위험도·도달시간을 만들지 못하게 막고,
응답의 수치·단계가 원자료와 맞는지 검사해서 틀리면 고정 문구(ai_fallback)로 둔다.

- 언제: 주의·위험·대피일 때만. 단계가 바뀔 때 + 같은 단계가 이어지면 30분마다 (수치가 바뀌니까)
  안전·판정 불가는 고정 문구 (AI 안 부름 → 비용 거의 없음)
- 검사: 숫자는 넘겨준 사실에 있는 것만, 단계 이름은 지금 단계만, "○분 후/뒤" 같은 도착 예측 금지, 길이 제한
  실패하면 한 번 더 시도, 그래도 실패하면 고정 문구
- 키가 없거나 API 장애여도 판정·푸시는 그대로 (judge가 예외를 따로 잡음)

환경변수: ANTHROPIC_API_KEY (없으면 AI 설명 건너뜀), ANTHROPIC_MODEL (기본 claude-haiku-5-5)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

log = logging.getLogger("explain")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-5-5"
TIMEOUT = 20
MAX_CHARS = 160
ATTEMPTS = 2
REFRESH_AFTER = timedelta(minutes=30)

ALERT_LEVELS = ("caution", "danger", "evacuate")
LEVEL_KO = {"safe": "안전", "caution": "주의", "danger": "위험", "evacuate": "대피", "unknown": "판정 불가"}
WINDOW_NUMBERS = {"1", "3", "24", "30", "72"}           # 1시간·3시간·24시간·30분·72시간
FORECAST = re.compile(r"\d+\s*(분|시간)\s*(후|뒤)|도착|도달")
NUMBER = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# 사실 정리 (판정 결과 → AI에 넘길 줄)
# ---------------------------------------------------------------------------

@dataclass
class Facts:
    name: str
    level: str
    lines: list[str] = field(default_factory=list)      # AI에 그대로 보여줄 사실
    numbers: set[str] = field(default_factory=set)       # 응답에 나와도 되는 숫자


def _fmt(v, places: int) -> str:
    return f"{Decimal(v):.{places}f}"


def _pick(value, limits: dict) -> Decimal | None:
    """값이 넘은 가장 높은 단계의 기준값. 아무것도 안 넘었으면 '주의' 기준값 (앱 화면과 같은 규칙)."""
    best = limits.get("caution")
    if value is not None:
        for lvl in ALERT_LEVELS:
            lim = limits.get(lvl)
            if lim is not None and value >= lim:
                best = lim
    return best


def build_facts(name: str, verdict, thresholds) -> Facts:
    """thresholds: [(level, rain_1h_mm, water_rise_m)] 또는 judge.Threshold 목록."""
    rows = [(t.level, t.rain_1h_mm, t.water_rise_m) if hasattr(t, "level") else tuple(t) for t in thresholds]
    rain_lim = {r[0]: r[1] for r in rows}
    rise_lim = {r[0]: r[2] for r in rows}
    f = Facts(name, verdict.level)
    f.lines.append(f"계곡: {name}")
    f.lines.append(f"지금 단계: {LEVEL_KO[verdict.level]}")

    def add(label, value, lim, unit, places, why=None):
        v, t = _fmt(value, places), _fmt(lim, places) if lim is not None else None
        f.numbers.update({v} | ({t} if t else set()))
        f.lines.append(f"{label}: {v}{unit}" + (f" (기준 {t}{unit})" if t else "") + (f" → {why}" if why else ""))

    rain, rise, est = verdict.rain, verdict.rise, getattr(verdict, "est", None)
    if rain.value is not None:
        lim = _pick(rain.value, rain_lim)
        add("상류 1시간 강수", rain.value, lim, "mm", 1,
            "기준을 넘음" if lim is not None and rain.value >= lim else "기준 아래")
    else:
        f.lines.append(f"상류 1시간 강수: 자료 없음 ({rain.reason})")
    if rise.value is not None:
        lim = _pick(rise.value, rise_lim)
        add("하류 30분 수위 상승", rise.value, lim, "m", 2,
            "기준을 넘음" if lim is not None and rise.value >= lim else "기준 아래")
    elif est is None:
        f.lines.append(f"하류 30분 수위 상승: 자료 없음 ({rise.reason})")
    hold = getattr(verdict, "hold", None)
    if hold is not None and hold.value is not None:
        from app.judge import HOLD_M
        add("하류 수위가 최근 24시간 가장 낮을 때보다 높은 정도", hold.value, HOLD_M, "m", 2,
            "주의 기준을 넘음 (비가 그쳐도 물이 불어 있음)" if hold.value >= HOLD_M else "기준 아래")
    if est is not None:
        f.lines.append("하류 수위: 관측소가 없거나 자료가 끊겨서 AI가 강수로 추정함")
        if est.rain_24h is not None:
            add("24시간 강수", est.rain_24h, est.rule_mm, "mm", 1)
        if est.rise is not None:
            add("AI 추정 앞으로 3시간 수위 상승", est.rise, est.ai_m, "m", 2,
                "주의 기준을 넘음" if est.ai_hit else None)
    if getattr(verdict, "estimated", False):
        f.lines.append("이 단계는 실제 수위가 아니라 AI 추정으로 정해짐")
    return f


SYSTEM = (
    "너는 계곡 급류 경보 앱 '여울목'에서 경보 이유를 설명하는 역할이다. 판정은 규칙 엔진이 이미 끝냈다.\n"
    "아래 사실만 써서, 지금 단계가 왜 나왔는지 계곡 이용객에게 1~2문장, {max}자 이내로 설명하라.\n"
    "규칙:\n"
    "- 존댓말 '~요'체로 쓴다.\n"
    "- 사실에 적힌 숫자만 쓴다. 시각, 날짜, 새 숫자를 만들지 않는다.\n"
    "- 단계 이름은 '{level}'만 쓰고, 다른 단계 이름(안전·주의·위험·대피·판정 불가)은 쓰지 않는다.\n"
    "- 물이 언제 도착하는지, 몇 분 뒤에 어떻게 되는지 예측하지 않는다.\n"
    "- 행동 요령은 쓰지 않는다 (앱이 따로 보여준다). 공식 재난경보처럼 단정하지 않는다.\n"
    "- 설명 문장만 출력한다."
)


def prompt(f: Facts) -> tuple[str, str]:
    return SYSTEM.format(max=MAX_CHARS, level=LEVEL_KO[f.level]), "\n".join(f.lines)


# ---------------------------------------------------------------------------
# 검사
# ---------------------------------------------------------------------------

def _num(s: str) -> Decimal | None:
    try:
        return Decimal(s).normalize()
    except InvalidOperation:
        return None


def check(text: str, f: Facts, max_chars: int = MAX_CHARS, strict_levels: bool = True) -> str | None:
    """문제가 있으면 이유, 없으면 None.
    strict_levels=False(질문 답변용): '위험 신호가 없어요'처럼 일반 단어는 허용하고,
    '위험 단계'처럼 다른 단계를 가리키는 말만 막는다."""
    if not text:
        return "빈 응답"
    if len(text) > max_chars:
        return f"너무 김 ({len(text)}자)"
    allowed = {_num(n) for n in f.numbers | WINDOW_NUMBERS}
    allowed |= {abs(n) for n in allowed if n is not None}    # "-0.06m" 사실을 "0.06m 내려갔어요"로 써도 맞음 (#34)
    for n in NUMBER.findall(text):
        if _num(n) not in allowed:
            return f"사실에 없는 숫자 {n}"
    for lvl, ko in LEVEL_KO.items():
        if lvl == f.level:
            continue
        if (ko in text) if strict_levels or lvl == "unknown" else re.search(ko + r"\s*(단계|상태|경보)", text):
            return f"다른 단계 이름 '{ko}'"
    if FORECAST.search(text):
        return "도착 시간 예측"
    return None


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def ready() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def call_claude(system: str, user: str, client=None, max_tokens: int = 300) -> str:
    own = client is None
    if own:
        import httpx

        client = httpx.Client(timeout=TIMEOUT)
    try:
        r = client.post(API_URL, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }, json={
            "model": os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message")
            except Exception:
                msg = None
            raise RuntimeError(f"Claude API {r.status_code}: {msg}")
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    finally:
        if own:
            client.close()


def generate(f: Facts, client=None) -> tuple[str | None, str | None]:
    """(설명, 실패 이유). 설명이 None이면 고정 문구를 쓴다."""
    system, user = prompt(f)
    reason = None
    for _ in range(ATTEMPTS):
        try:
            text = call_claude(system, user, client)
        except Exception as e:
            return None, f"API 실패: {e}"
        reason = check(text, f)
        if reason is None:
            return text, None
        log.info("AI 설명 검사 실패 (%s): %s", reason, text)
    return None, reason


# ---------------------------------------------------------------------------
# 판정 엔진 연결
# ---------------------------------------------------------------------------

def should_explain(level: str, changed: bool, explained_at: datetime | None, now: datetime) -> bool:
    if level not in ALERT_LEVELS:
        return False
    return changed or explained_at is None or now - explained_at >= REFRESH_AFTER


def after_judge(conn, valley_id: int, name: str, verdict, thresholds, changed: bool, now: datetime) -> None:
    """판정 저장 직후 호출. 필요하면 설명을 만들어 최신 valley_status 행에 저장."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, ai_explained_at FROM valley_status WHERE valley_id = %s "
                    "ORDER BY changed_at DESC, id DESC LIMIT 1", (valley_id,))
        row = cur.fetchone()
    if not row or not should_explain(verdict.level, changed, row[1], now):
        return
    if not ready():
        return                                            # 키 없음 → 고정 문구 그대로
    text, reason = generate(build_facts(name, verdict, thresholds))
    if text:
        log.info("%s: AI 설명 저장 — %s", name, text)
    else:
        log.warning("%s: AI 설명 실패 → 고정 문구 (%s)", name, reason)
    with conn.cursor() as cur:
        cur.execute("UPDATE valley_status SET ai_explanation = %s, ai_fallback = %s, ai_explained_at = %s "
                    "WHERE id = %s", (text, text is None, now, row[0]))


# ---------------------------------------------------------------------------
# 시험:  python -m app.explain --test   (비가 안 와도 키·모델·검사를 확인, DB에 안 씀)
# ---------------------------------------------------------------------------

def _demo() -> int:
    from zoneinfo import ZoneInfo

    from app.judge import Signal, Threshold, decide

    now = datetime.now(ZoneInfo("Asia/Seoul"))
    th = [Threshold("caution", Decimal("15.0"), Decimal("0.10")),
          Threshold("danger", Decimal("25.0"), Decimal("0.20")),
          Threshold("evacuate", Decimal("40.0"), Decimal("0.30"))]
    v = decide(Signal(Decimal("18.0"), now), Signal(Decimal("0.06"), now), th)
    if not ready():
        print("ANTHROPIC_API_KEY 없음")
        return 1
    f = build_facts("금대리 계곡 (시험)", v, th)
    print("모델:", os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL))
    print("넘긴 사실:\n  " + "\n  ".join(f.lines))
    text, reason = generate(f)
    print("결과:", text if text else f"고정 문구로 대체 ({reason})")
    return 0 if text else 1


if __name__ == "__main__":
    import sys

    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if sys.argv[1:] == ["--test"]:
        sys.exit(_demo())
    print("사용법: python -m app.explain --test")
    sys.exit(2)
