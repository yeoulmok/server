"""경보 푸시 발송 (기능정의서 6절 푸시 규칙, API 명세 v0.2 4절 페이로드).

발송 규칙
- 단계가 올라갈 때 보냄: 직전의 '판정 불가가 아닌' 단계보다 높아지면 (안전→주의, 주의→위험, 위험→대피)
- 같은 단계 유지 · 단계가 내려갈 때 · 판정 불가: 안 보냄
- 안전 복귀: 마지막 발송이 경보(alert)이고, 지금 안전이 30분 이상 이어졌으면 1회
- 판정 불가가 끼어도 단계 비교에서 건너뛴다 (주의 → 판정 불가 → 주의 는 재발송 안 함)
- 야간(22~06시 KST) 주의·안전 복귀는 소리 없이. 위험·대피는 항상 소리 (단계별 소리 파일, 앱 PR #86)

토큰 종류 (11/20 TestFlight): Xcode 빌드 = sandbox, TestFlight·App Store = production. 토큰만 봐선 모름.
→ 처음 보는 토큰은 APNS_ENV 쪽(기본 production)으로 먼저 보내고, Apple이 400 BadDeviceToken이면 다른 쪽으로
  다시 보냄. 성공한 쪽을 devices.apns_env에 기억해서 다음부터 바로 그쪽으로. 두 쪽 다 거절해야 토큰을 지움.
  앱이 토큰을 새로 등록하면(PATCH /devices) apns_env는 비움.
- 토큰마다 Apple 응답을 로그와 push_log.detail에 남김 (예: "…a1b2c3 production 400 BadDeviceToken → sandbox 200")

APNs 키(APNS_KEY_ID 등)가 없으면 실제 발송 없이 로그와 push_log(dry_run=true)만 남긴다.

APNs 환경변수 (~/secrets/yeoulmok.env)
  APNS_KEY_ID    Apple Developer에서 받은 키 ID (10자리)
  APNS_TEAM_ID   팀 ID (10자리)
  APNS_KEY_PATH  컨테이너 안 .p8 파일 경로 (예: /run/secrets/apns.p8, 읽기 전용 마운트)
  APNS_TOPIC     앱 번들 ID
  APNS_ENV       처음 보는 토큰을 먼저 보낼 쪽. production(기본) | sandbox. 안 맞으면 다른 쪽으로 자동 재시도
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


ALARM_LEVELS = ("danger", "evacuate")
SOUNDS = {"evacuate": "evacuate.caf", "danger": "danger.caf",   # 앱 번들 소리 (iOS 앱 PR #86)
          "caution": "caution.caf", "safe": "default"}


def payload(valley_id: int, name: str, level: str, now: datetime) -> dict:
    """위험·대피: 단계별 소리 + Time Sensitive(집중 모드 통과), 밤에도.
    주의: caution.caf, 안전 복귀: 기본 소리. 둘 다 밤(22~06시)엔 무음. (Critical Alert는 Apple 승인 전이라 안 씀)"""
    aps: dict = {"alert": {"title": f"{name} · {LEVEL_KO[level]}", "body": TEXTS[level][0]}}
    if level in ALARM_LEVELS:
        aps["sound"] = SOUNDS[level]
        aps["interruption-level"] = "time-sensitive"
    else:
        if not is_night(now):
            aps["sound"] = SOUNDS[level]
        aps["interruption-level"] = "active"
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


def _targets(cur, valley_id: int) -> list[tuple[str, str | None]]:
    """(토큰, 기억해 둔 환경 또는 None)"""
    cur.execute("SELECT d.apns_token, d.apns_env FROM subscriptions s JOIN devices d ON d.id = s.device_id "
                "WHERE s.valley_id = %s AND d.notify_on AND d.apns_token IS NOT NULL", (valley_id,))
    return [(r[0], r[1]) for r in cur.fetchall()]


APNS_ENV_KEYS = ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_KEY_PATH", "APNS_TOPIC")
APNS_HOSTS = {"production": "https://api.push.apple.com",       # TestFlight · App Store build
              "sandbox": "https://api.sandbox.push.apple.com"}  # build run directly from Xcode
TOKEN_TTL = timedelta(minutes=50)       # Apple: provider token valid 1h, refresh at most every 20 min
EXPIRE_AFTER = timedelta(minutes=30)    # stale warnings are worse than none: drop if undelivered
DEAD_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


def _apns_ready() -> bool:
    return all(os.environ.get(k) for k in APNS_ENV_KEYS)


def first_env() -> str:
    env = os.environ.get("APNS_ENV", "production")
    return env if env in APNS_HOSTS else "production"


def apns_host() -> str:
    return APNS_HOSTS[first_env()]


def env_order(known: str | None) -> list[str]:
    """기억해 둔 쪽(없으면 APNS_ENV 쪽) 먼저, 그다음 다른 쪽."""
    first = known if known in APNS_HOSTS else first_env()
    return [first] + [e for e in APNS_HOSTS if e != first]


@dataclass
class SendResult:
    ok: int = 0
    dead: list = None           # 지울 토큰 (두 쪽 다 거절 · 410 등)
    learned: dict = None        # 토큰 → 성공한 환경 (기억과 다를 때만)
    detail: list = None         # 토큰마다 Apple 응답 한 줄

    def __post_init__(self):
        self.dead, self.learned, self.detail = self.dead or [], self.learned or {}, self.detail or []


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


def send(targets, body: dict, now: datetime, client=None) -> SendResult:
    """실제 APNs 발송. targets: [(토큰, 기억한 환경|None)] (토큰 문자열만 줘도 됨).
    BadDeviceToken이면 다른 환경으로 한 번 더. 토큰 하나가 실패해도 나머지는 계속 보낸다.
    client: 테스트용 HTTP 클라이언트 (post(url, headers=, json=) → status_code, json())."""
    own = client is None
    if own:
        import httpx

        client = httpx.Client(http2=True, timeout=10)
    res = SendResult()
    try:
        headers = apns_headers(_provider_token(now), os.environ["APNS_TOPIC"], body["valley_id"], now)
        for t in targets:
            token, known = (t, None) if isinstance(t, str) else t
            steps, verdict = [], None
            for env in env_order(known):
                try:
                    r = client.post(f"{APNS_HOSTS[env]}/3/device/{token}", headers=headers, json=body)
                except Exception as e:  # noqa: BLE001 — 네트워크 오류: 토큰은 그대로 두고 다음 토큰
                    steps.append(f"{env} 요청 실패 {type(e).__name__}")
                    verdict = "error"
                    break
                if r.status_code == 200:
                    steps.append(f"{env} 200")
                    res.ok += 1
                    if env != known:
                        res.learned[token] = env
                    verdict = "ok"
                    break
                try:
                    reason = r.json().get("reason")
                except Exception:  # noqa: BLE001
                    reason = None
                steps.append(f"{env} {r.status_code} {reason}")
                if reason in ("ExpiredProviderToken", "InvalidProviderToken"):
                    _token_cache.update(token=None, made_at=None)
                if reason == "BadDeviceToken":
                    verdict = "dead"          # 다른 쪽도 거절하면 지움
                    continue
                verdict = "dead" if (reason in DEAD_REASONS or r.status_code == 410) else "error"
                break
            if verdict == "dead":
                res.dead.append(token)
            line = f"…{token[-6:]} " + " → ".join(steps)
            res.detail.append(line)
            (log.info if verdict == "ok" else log.warning)("APNs %s", line)
    finally:
        if own:
            client.close()
    return res


def _remember_env(cur, learned: dict) -> None:
    for token, env in learned.items():
        cur.execute("UPDATE devices SET apns_env = %s WHERE apns_token = %s", (env, token))


def _forget_tokens(cur, tokens: list[str]) -> None:
    """앱을 지웠거나 토큰이 바뀐 기기 → 토큰만 지운다 (구독은 그대로, 앱이 다시 3번 PATCH로 넣으면 됨)."""
    if tokens:
        cur.execute("UPDATE devices SET apns_token = NULL, apns_env = NULL WHERE apns_token = ANY(%s)", (tokens,))


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
        detail = None
        if dry:
            log.info("[dry-run] %s 푸시 %s → 기기 %d대: %s", plan.kind, name, len(tokens), body["aps"])
        elif tokens:
            try:
                res = send(tokens, body, now)
                log.info("%s 푸시 %s → %d/%d대 성공%s", plan.kind, name, res.ok, len(tokens),
                         f", 토큰 {len(res.dead)}개 삭제" if res.dead else "")
                _remember_env(cur, res.learned)
                _forget_tokens(cur, res.dead)
                detail = "; ".join(res.detail)
            except Exception:
                log.exception("%s 푸시 발송 실패 (%s)", plan.kind, name)
        cur.execute("INSERT INTO push_log (valley_id, kind, level, devices, dry_run, detail) "
                    "VALUES (%s, %s, %s, %s, %s, %s)", (valley_id, plan.kind, plan.level, len(tokens), dry, detail))
    return plan


# ---------------------------------------------------------------------------
# 시험 발송:  python -m app.push --test <device_id>
# ---------------------------------------------------------------------------

def test_send(device_id: str, level: str | None = None) -> int:
    """level을 주면 실제 경보와 같은 내용(소리·interruption-level 포함)으로 보냄. 단계가 바뀌거나 기록이 남지는 않음."""
    from app.judge import connect

    now = datetime.now(KST)
    if not _apns_ready():
        print("APNs 환경변수가 없음:", [k for k in APNS_ENV_KEYS if not os.environ.get(k)])
        return 1
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT apns_token, apns_env FROM devices WHERE id = %s", (device_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            print("이 기기에 APNs 토큰이 없음:", device_id)
            return 1
        if level:
            body = payload(0, "여울목 시험", level, now)
        else:
            body = {"aps": {"alert": {"title": "여울목 · 시험 알림", "body": "푸시 연결 확인용이에요"},
                            "sound": "default"}, "valley_id": 0, "level": "test"}
        res = send([(row[0], row[1])], body, now)
        _remember_env(cur, res.learned)
        _forget_tokens(cur, res.dead)
    print(f"Apple 응답: {res.detail[0]}")
    print(f"→ 성공 {res.ok}/1" + (" (두 쪽 다 거절 → 토큰 삭제함. 앱을 다시 열면 새로 등록됨)" if res.dead else ""))
    return 0 if res.ok else 1


if __name__ == "__main__":
    import sys

    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--test":
        lvl = sys.argv[3] if len(sys.argv) == 4 else None
        if lvl and lvl not in LEVEL_KO:
            sys.exit(f"단계는 {', '.join(LEVEL_KO)} 중 하나")
        sys.exit(test_send(sys.argv[2], lvl))
    print("사용법: python -m app.push --test <device_id> [caution|danger|evacuate]")
    sys.exit(2)
