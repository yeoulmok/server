"""경보 푸시 발송 (기능정의서 6절 푸시 규칙, API 명세 v0.2 4절 페이로드).

발송 규칙
- 단계가 올라갈 때 보냄: 직전의 '판정 불가가 아닌' 단계보다 높아지면 (안전→주의, 주의→위험, 위험→대피)
- 같은 단계 유지 · 단계가 내려갈 때 · 판정 불가: 안 보냄
- 안전 복귀: 마지막 발송이 경보(alert)이고, 지금 안전이 30분 이상 이어졌으면 1회
- 판정 불가가 끼어도 단계 비교에서 건너뛴다 (주의 → 판정 불가 → 주의 는 재발송 안 함)
- 야간(22~06시 KST) 주의는 소리 없이. 위험·대피는 항상 소리

APNs 키(APNS_KEY_ID 등)가 없으면 실제 발송 없이 로그와 push_log(dry_run=true)만 남긴다.

APNs 환경변수 (~/secrets/yeoulmok.env)
  APNS_KEY_ID    Apple Developer에서 받은 키 ID (10자리)
  APNS_TEAM_ID   팀 ID (10자리)
  APNS_KEY_PATH  컨테이너 안 .p8 파일 경로 (예: /run/secrets/apns.p8, 읽기 전용 마운트)
  APNS_TOPIC     앱 번들 ID
  APNS_ENV       production(기본, TestFlight·App Store) | sandbox(Xcode에서 바로 설치한 빌드)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.valley_view import TEXTS

log = logging.getLogger("push")
# httpx logs every request URL at INFO, and the APNs URL contains the full device token
logging.getLogger("httpx").setLevel(logging.WARNING)

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


APNS_ENV_KEYS = ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_KEY_PATH", "APNS_TOPIC")
APNS_HOSTS = {"production": "https://api.push.apple.com",       # TestFlight · App Store build
              "sandbox": "https://api.sandbox.push.apple.com"}  # build run directly from Xcode
TOKEN_TTL = timedelta(minutes=50)       # Apple: provider token valid 1h, refresh at most every 20 min
EXPIRE_AFTER = timedelta(minutes=30)    # stale warnings are worse than none: drop if undelivered
DEAD_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


def _apns_ready() -> bool:
    return all(os.environ.get(k) for k in APNS_ENV_KEYS)


def apns_host() -> str:
    return APNS_HOSTS.get(os.environ.get("APNS_ENV", "production"), APNS_HOSTS["production"])


def make_token(key_pem: str, key_id: str, team_id: str, now: datetime) -> str:
    """APNs provider token (JWT, ES256) signed with the .p8 key."""
    import jwt

    return jwt.encode({"iss": team_id, "iat": int(now.timestamp())}, key_pem,
                      algorithm="ES256", headers={"kid": key_id})


_token_cache: dict = {"token": None, "made_at": None}


def _provider_token(now: datetime) -> str:
    made = _token_cache["made_at"]
    if _token_cache["token"] is None or made is None or now - made >= TOKEN_TTL:
        with open(os.environ["APNS_KEY_PATH"], encoding="utf-8") as f:
            key_pem = f.read()
        _token_cache.update(token=make_token(key_pem, os.environ["APNS_KEY_ID"],
                                             os.environ["APNS_TEAM_ID"], now), made_at=now)
    return _token_cache["token"]


def apns_headers(provider_token: str, topic: str, valley_id: int, now: datetime) -> dict:
    return {
        "authorization": f"bearer {provider_token}",
        "apns-topic": topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-expiration": str(int((now + EXPIRE_AFTER).timestamp())),
        "apns-collapse-id": f"valley-{valley_id}",   # newer alert for the same valley replaces the older one
    }


def send(tokens: list[str], body: dict, now: datetime, client=None) -> tuple[int, list[str]]:
    """실제 APNs 발송. (성공 수, 지워야 할 토큰 목록). 토큰 하나가 실패해도 나머지는 계속 보낸다.
    client: 테스트용으로 바꿔 끼울 수 있는 HTTP 클라이언트 (post(url, headers=, json=) → status_code, json())."""
    own = client is None
    if own:
        import httpx

        client = httpx.Client(http2=True, timeout=10)
    ok, dead = 0, []
    try:
        headers = apns_headers(_provider_token(now), os.environ["APNS_TOPIC"], body["valley_id"], now)
        for token in tokens:
            try:
                r = client.post(f"{apns_host()}/3/device/{token}", headers=headers, json=body)
            except Exception:
                log.exception("APNs 요청 실패 (토큰 …%s)", token[-6:])
                continue
            if r.status_code == 200:
                ok += 1
                continue
            try:
                reason = r.json().get("reason")
            except Exception:
                reason = None
            log.warning("APNs 거절 %s %s (토큰 …%s)", r.status_code, reason, token[-6:])
            if reason in DEAD_REASONS or r.status_code == 410:
                dead.append(token)
            if reason in ("ExpiredProviderToken", "InvalidProviderToken"):
                _token_cache.update(token=None, made_at=None)
    finally:
        if own:
            client.close()
    return ok, dead


def _forget_tokens(cur, tokens: list[str]) -> None:
    """앱을 지웠거나 토큰이 바뀐 기기 → 토큰만 지운다 (구독은 그대로, 앱이 다시 3번 PATCH로 넣으면 됨)."""
    if tokens:
        cur.execute("UPDATE devices SET apns_token = NULL WHERE apns_token = ANY(%s)", (tokens,))


def after_judge(conn, valley_id: int, name: str, now: datetime) -> Plan | None:
    """판정 직후 호출. 보낼 게 있으면 보내고(키 없으면 dry-run) push_log에 남긴다.
    발송이 실패해도 push_log는 남긴다 → 5분마다 같은 알림을 다시 보내지 않게."""
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
            try:
                ok, dead = send(tokens, body, now)
                log.info("%s 푸시 %s → %d/%d대 성공%s", plan.kind, name, ok, len(tokens),
                         f", 토큰 {len(dead)}개 삭제" if dead else "")
                _forget_tokens(cur, dead)
            except Exception:
                log.exception("%s 푸시 발송 실패 (%s)", plan.kind, name)
        cur.execute("INSERT INTO push_log (valley_id, kind, level, devices, dry_run) "
                    "VALUES (%s, %s, %s, %s, %s)", (valley_id, plan.kind, plan.level, len(tokens), dry))
    return plan


# ---------------------------------------------------------------------------
# 시험 발송:  python -m app.push --test <device_id>
# ---------------------------------------------------------------------------

def test_send(device_id: str) -> int:
    from app.judge import connect

    now = datetime.now(KST)
    if not _apns_ready():
        print("APNs 환경변수가 없음:", [k for k in APNS_ENV_KEYS if not os.environ.get(k)])
        return 1
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT apns_token FROM devices WHERE id = %s", (device_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            print("이 기기에 APNs 토큰이 없음:", device_id)
            return 1
        body = {"aps": {"alert": {"title": "여울목 · 시험 알림", "body": "푸시 연결 확인용이에요"},
                        "sound": "default"}, "valley_id": 0, "level": "test"}
        ok, dead = send([row[0]], body, now)
        _forget_tokens(cur, dead)
    print(f"서버: {apns_host()} → 성공 {ok}/1" + (" (토큰 무효 → 삭제함)" if dead else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        sys.exit(test_send(sys.argv[2]))
    print("사용법: python -m app.push --test <device_id>")
    sys.exit(2)
