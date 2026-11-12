"""경보 푸시 발송 (기능정의서 6절 푸시 규칙, API 명세 v0.2 4절 페이로드).

발송 규칙
- 단계가 올라갈 때 보냄: 직전의 '판정 불가가 아닌' 단계보다 높아지면 (안전→주의, 주의→위험, 위험→대피)
- 같은 단계 유지 · 단계가 내려갈 때 · 판정 불가: 안 보냄
- 안전 복귀: 마지막 발송이 경보(alert)이고, 지금 안전이 30분 이상 이어졌으면 1회
- 판정 불가가 끼어도 단계 비교에서 건너뛴다 (주의 → 판정 불가 → 주의 는 재발송 안 함)
- 야간(22~06시 KST) 주의는 소리 없이. 위험·대피는 항상 소리

APNs 키(APNS_KEY_ID 등)가 없으면 실제 발송 없이 로그와 push_log(dry_run=true)만 남긴다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.valley_view import TEXTS

log = logging.getLogger("push")

KST = ZoneInfo("Asia/Seoul")
RANK = {"safe": 0, "caution": 1, "danger": 2, "evacuate": 3}
LEVEL_KO = {"safe": "안전", "caution": "주의", "danger": "위험", "evacuate": "대피"}
RECOVERY_AFTER = timedelta(minutes=30)
NIGHT_START, NIGHT_END = 22, 6


@dataclass(frozen=True)
class Plan:
    kind: str      # 'alert' | 'recovery'
    level: str


# ---------------------------------------------------------------------------
# 순수 판단 (DB 없이 테스트)
# ---------------------------------------------------------------------------

def decide(current: str, current_since: datetime, prev_known: str | None,
           last_sent_kind: str | None, sent_in_this_stage: bool, now: datetime) -> Plan | None:
    """current: 지금 단계, current_since: 지금 단계가 시작된 시각,
    prev_known: 지금 단계 직전의 '판정 불가가 아닌' 단계 (없으면 None = 안전으로 봄),
    last_sent_kind: 이 계곡의 마지막 발송 종류 (없으면 None),
    sent_in_this_stage: 지금 단계가 시작된 뒤 이미 경보를 보냈는지 (재시작·주기 겹침 대비)."""
    if current not in RANK:                       # unknown 등
        return None
    if current != "safe":
        before = RANK.get(prev_known or "safe", 0)
        if RANK[current] > before and not sent_in_this_stage:
            return Plan("alert", current)
        return None
    if last_sent_kind == "alert" and now - current_since >= RECOVERY_AFTER:
        return Plan("recovery", "safe")
    return None


def is_night(now: datetime) -> bool:
    h = now.astimezone(KST).hour
    return h >= NIGHT_START or h < NIGHT_END


def payload(valley_id: int, name: str, level: str, now: datetime) -> dict:
    aps: dict = {"alert": {"title": f"{name} · {LEVEL_KO[level]}", "body": TEXTS[level][0]}}
    if not (level in ("safe", "caution") and is_night(now)):
        aps["sound"] = "default"
    return {"aps": aps, "valley_id": valley_id, "level": level}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _state(cur, valley_id: int):
    cur.execute("SELECT level, changed_at FROM valley_status WHERE valley_id = %s "
                "ORDER BY changed_at DESC, id DESC LIMIT 50", (valley_id,))
    rows = cur.fetchall()
    if not rows:
        return None
    current, since = rows[0]
    prev_known = next((lvl for lvl, _ in rows[1:] if lvl in RANK), None)
    cur.execute("SELECT kind, level, created_at FROM push_log WHERE valley_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (valley_id,))
    last = cur.fetchone()
    last_kind = last[0] if last else None
    sent_in_stage = bool(last and last[0] == "alert" and last[1] == current and last[2] >= since)
    return current, since, prev_known, last_kind, sent_in_stage


def _targets(cur, valley_id: int) -> list[str]:
    cur.execute("SELECT d.apns_token FROM subscriptions s JOIN devices d ON d.id = s.device_id "
                "WHERE s.valley_id = %s AND d.notify_on AND d.apns_token IS NOT NULL", (valley_id,))
    return [r[0] for r in cur.fetchall()]


def _apns_ready() -> bool:
    return all(os.environ.get(k) for k in ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_KEY_PATH", "APNS_TOPIC"))


def send(tokens: list[str], body: dict) -> None:
    """실제 APNs 발송. 인증키를 받으면 구현한다."""
    raise NotImplementedError("APNs 발송은 인증키를 받은 뒤 구현")


def after_judge(conn, valley_id: int, name: str, now: datetime) -> Plan | None:
    """판정 직후 호출. 보낼 게 있으면 보내고(키 없으면 dry-run) push_log에 남긴다."""
    with conn.cursor() as cur:
        st = _state(cur, valley_id)
        if not st:
            return None
        plan = decide(*st, now)
        if not plan:
            return None
        tokens = _targets(cur, valley_id)
        body = payload(valley_id, name, plan.level, now)
        dry = not _apns_ready()
        if dry:
            log.info("[dry-run] %s 푸시 %s → 기기 %d대: %s", plan.kind, name, len(tokens), body["aps"])
        elif tokens:
            send(tokens, body)
        cur.execute("INSERT INTO push_log (valley_id, kind, level, devices, dry_run) "
                    "VALUES (%s, %s, %s, %s, %s)", (valley_id, plan.kind, plan.level, len(tokens), dry))
    return plan
