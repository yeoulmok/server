"""기기·구독 API 통합 테스트 (실제 Postgres 필요).

실행: DB_HOST=... POSTGRES_USER=... POSTGRES_PASSWORD=... POSTGRES_DB=... python tests/test_api_devices.py
schema.sql + seed.sql이 들어간 DB에서 돌린다. 테스트로 만든 기기는 끝나고 지운다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402

from app.judge import connect  # noqa: E402
from app.main import app  # noqa: E402

c = TestClient(app)
made: list[str] = []


def new_device(token="tok123"):
    r = c.post("/api/devices", json={"apns_token": token})
    assert r.status_code == 201, r.text
    did = r.json()["device_id"]
    made.append(did)
    return did


def test_register_and_patch():
    did = new_device()
    assert did.startswith("d_")
    r = c.patch("/api/devices", json={"notify_on": False}, headers={"X-Device-Id": did})
    assert r.status_code == 200 and r.json()["notify_on"] is False and r.json()["has_apns_token"]
    r = c.patch("/api/devices", json={"apns_token": "new"}, headers={"X-Device-Id": did})
    assert r.json()["notify_on"] is False          # 안 보낸 필드는 그대로


def test_errors():
    assert c.patch("/api/devices", json={}).json()["error"]["code"] == "DEVICE_ID_REQUIRED"
    r = c.patch("/api/devices", json={}, headers={"X-Device-Id": "d_nope"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "DEVICE_NOT_FOUND"
    did = new_device()
    r = c.put("/api/me/subscriptions/9999", headers={"X-Device-Id": did})
    assert r.status_code == 404 and r.json()["error"]["code"] == "VALLEY_NOT_FOUND"


def test_subscribe_flow():
    did = new_device()
    h = {"X-Device-Id": did}
    assert c.get("/api/me/valleys", headers=h).json()["valleys"] == []
    assert c.put("/api/me/subscriptions/1", headers=h).json()["subscribed"] is True
    assert c.put("/api/me/subscriptions/1", headers=h).status_code == 200    # 두 번 해도 OK
    mine = c.get("/api/me/valleys", headers=h).json()
    assert [v["id"] for v in mine["valleys"]] == [1] and mine["valleys"][0]["subscribed"] is True
    assert "source" in mine
    assert c.get("/api/valleys", headers=h).json()["valleys"][0]["subscribed"] is True
    assert c.delete("/api/me/subscriptions/1", headers=h).json()["subscribed"] is False
    assert c.get("/api/me/valleys", headers=h).json()["valleys"] == []


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
                print("ok", name)
    finally:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM devices WHERE id = ANY(%s)", (made,))
