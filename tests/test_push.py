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
    assert payload(1, "금대리 계곡", "danger", night)["aps"]["sound"] == "default"
    assert "sound" not in payload(1, "금대리 계곡", "caution", NOW.replace(hour=5))["aps"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
