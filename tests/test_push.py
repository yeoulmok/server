"""푸시 발송 조건 단위 테스트 (DB 불필요).  실행: python tests/test_push.py"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.push import Plan, decide, payload  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 12, 14, 0, tzinfo=KST)
M = timedelta(minutes=1)


def test_up_sends():
    assert decide("caution", NOW, "safe", None, False, NOW) == Plan("alert", "caution")
    assert decide("danger", NOW, "caution", "alert", False, NOW) == Plan("alert", "danger")
    assert decide("evacuate", NOW, "danger", "alert", False, NOW) == Plan("alert", "evacuate")


def test_skip_up_sends_once():
    assert decide("danger", NOW, "safe", None, False, NOW) == Plan("alert", "danger")


def test_no_resend_same_stage():
    assert decide("caution", NOW - 10 * M, "safe", "alert", True, NOW) is None


def test_down_no_send():
    assert decide("caution", NOW, "danger", "alert", False, NOW) is None


def test_unknown_never_sends():
    assert decide("unknown", NOW, "caution", "alert", False, NOW) is None


def test_unknown_blip_no_resend():
    # 주의 → 판정 불가 → 주의: 직전 '판정 불가 아닌' 단계가 주의라 재발송 안 함
    assert decide("caution", NOW, "caution", "alert", False, NOW) is None


def test_unknown_then_higher_sends():
    assert decide("danger", NOW, "caution", "alert", False, NOW) == Plan("alert", "danger")


def test_first_status_from_nothing():
    assert decide("caution", NOW, None, None, False, NOW) == Plan("alert", "caution")
    assert decide("safe", NOW - 60 * M, None, None, False, NOW) is None


def test_recovery_after_30min():
    assert decide("safe", NOW - 29 * M, "danger", "alert", False, NOW) is None
    assert decide("safe", NOW - 30 * M, "danger", "alert", False, NOW) == Plan("recovery", "safe")


def test_recovery_once():
    assert decide("safe", NOW - 60 * M, "danger", "recovery", False, NOW) is None


def test_recovery_survives_unknown_blip():
    # 위험(발송) → 안전 → 판정 불가 → 안전 30분: 마지막 발송이 경보라 복귀 알림 보냄
    assert decide("safe", NOW - 30 * M, "safe", "alert", False, NOW) == Plan("recovery", "safe")


def test_payload_and_night_sound():
    day = payload(1, "금대리 계곡", "caution", NOW)
    assert day["aps"]["alert"]["title"] == "금대리 계곡 · 주의" and day["aps"]["sound"] == "default"
    assert day["valley_id"] == 1 and day["level"] == "caution"
    night = NOW.replace(hour=23)
    assert "sound" not in payload(1, "금대리 계곡", "caution", night)["aps"]
    assert payload(1, "금대리 계곡", "danger", night)["aps"]["sound"] == "alarm.caf"
    assert "sound" not in payload(1, "금대리 계곡", "caution", NOW.replace(hour=5))["aps"]




def test_alarm_levels_time_sensitive_day_and_night():
    night = NOW.replace(hour=23)
    for lvl in ("danger", "evacuate"):
        for t in (NOW, night):
            aps = payload(1, "금대리 계곡", lvl, t)["aps"]
            assert aps["sound"] == "alarm.caf" and aps["interruption-level"] == "time-sensitive"
    for lvl in ("caution", "safe"):
        assert payload(1, "금대리 계곡", lvl, NOW)["aps"]["interruption-level"] == "active"
        assert payload(1, "금대리 계곡", lvl, NOW)["aps"]["sound"] == "default"
        aps = payload(1, "금대리 계곡", lvl, night)["aps"]
        assert "sound" not in aps and aps["interruption-level"] == "active"     # 밤엔 무음 그대로

# ---------------------------------------------------------------------------
# APNs 실제 발송 (가짜 애플 서버로)
# ---------------------------------------------------------------------------

import os  # noqa: E402
import tempfile  # noqa: E402

from app import push  # noqa: E402


def _ec_key_pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return key, pem


def test_provider_token_is_es256_with_kid():
    import jwt

    key, pem = _ec_key_pem()
    tok = push.make_token(pem, "ABC123DEFG", "TEAM123456", NOW)
    assert jwt.get_unverified_header(tok) == {"alg": "ES256", "kid": "ABC123DEFG", "typ": "JWT"}
    claims = jwt.decode(tok, key.public_key(), algorithms=["ES256"])
    assert claims == {"iss": "TEAM123456", "iat": int(NOW.timestamp())}


class _Resp:
    def __init__(self, code, reason=None):
        self.status_code, self._reason = code, reason

    def json(self):
        return {"reason": self._reason} if self._reason else {}


class _FakeApple:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def post(self, url, headers, json):
        self.calls.append((url, headers, json))
        ans = self.answers[url.rsplit("/", 1)[1]]
        if isinstance(ans, Exception):
            raise ans
        return ans


def _with_env(fn):
    _, pem = _ec_key_pem()
    with tempfile.NamedTemporaryFile("w", suffix=".p8", delete=False) as f:
        f.write(pem)
    env = {"APNS_KEY_ID": "ABC123DEFG", "APNS_TEAM_ID": "TEAM123456", "APNS_KEY_PATH": f.name,
           "APNS_TOPIC": "com.yeoulmok.app"}
    old = {k: os.environ.get(k) for k in list(env) + ["APNS_ENV"]}
    os.environ.update(env)
    os.environ.pop("APNS_ENV", None)
    push._token_cache.update(token=None, made_at=None)
    try:
        fn()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.unlink(f.name)


def test_send_counts_and_collects_dead_tokens():
    def run():
        apple = _FakeApple({"good": _Resp(200), "gone": _Resp(410, "Unregistered"),
                            "bad": _Resp(400, "BadDeviceToken"), "busy": _Resp(503, "ServiceUnavailable"),
                            "net": OSError("connection reset")})
        body = push.payload(1, "금대리 계곡", "danger", NOW)
        ok, dead = push.send(["good", "gone", "bad", "busy", "net"], body, NOW, client=apple)
        assert ok == 1 and sorted(dead) == ["bad", "gone"]           # 일시 장애(503·끊김)는 토큰 유지
        url, headers, sent = apple.calls[0]
        assert url == "https://api.push.apple.com/3/device/good"     # 기본은 production
        assert headers["apns-topic"] == "com.yeoulmok.app" and headers["apns-push-type"] == "alert"
        assert headers["authorization"].startswith("bearer ") and headers["apns-collapse-id"] == "valley-1"
        assert int(headers["apns-expiration"]) == int((NOW + push.EXPIRE_AFTER).timestamp())
        assert sent == body
    _with_env(run)


def test_sandbox_host():
    def run():
        os.environ["APNS_ENV"] = "sandbox"
        apple = _FakeApple({"t": _Resp(200)})
        push.send(["t"], push.payload(1, "금대리 계곡", "caution", NOW), NOW, client=apple)
        assert apple.calls[0][0].startswith("https://api.sandbox.push.apple.com/")
    _with_env(run)


def test_token_reused_then_refreshed():
    def run():
        apple = _FakeApple({"t": _Resp(200)})
        body = push.payload(1, "금대리 계곡", "caution", NOW)
        push.send(["t"], body, NOW, client=apple)
        push.send(["t"], body, NOW + timedelta(minutes=10), client=apple)
        push.send(["t"], body, NOW + timedelta(minutes=55), client=apple)
        auth = [h["authorization"] for _, h, _ in apple.calls]
        assert auth[0] == auth[1] != auth[2]                         # 50분 안엔 재사용, 넘으면 새로
    _with_env(run)


def test_expired_provider_token_forces_refresh():
    def run():
        apple = _FakeApple({"t": _Resp(403, "ExpiredProviderToken")})
        push.send(["t"], push.payload(1, "금대리 계곡", "caution", NOW), NOW, client=apple)
        assert push._token_cache["token"] is None
    _with_env(run)

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
