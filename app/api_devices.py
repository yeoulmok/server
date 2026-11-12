"""API 명세 v0.2 — 2·3번 기기, 6번 내 계곡, 7·8번 구독.

로그인 없음. 2번에서 받은 device_id를 이후 요청 헤더 X-Device-Id로 보낸다.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api_valleys import _not_found, _valley_list
from app.judge import connect

router = APIRouter(prefix="/api")


class DeviceCreate(BaseModel):
    apns_token: str | None = None
    platform: str = "ios"


class DeviceUpdate(BaseModel):
    apns_token: str | None = None
    notify_on: bool | None = None


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


# message is shown to users as-is (iOS error screen title) — no internal IDs or paths.
def _no_device_header() -> JSONResponse:
    return _error(400, "DEVICE_ID_REQUIRED", "기기 정보를 확인할 수 없어요")


def _device_not_found() -> JSONResponse:
    return _error(404, "DEVICE_NOT_FOUND", "기기 정보를 확인할 수 없어요")


def _touch_device(cur, device_id: str) -> bool:
    """기기가 있으면 last_seen_at 갱신 후 True."""
    cur.execute("UPDATE devices SET last_seen_at = now() WHERE id = %s", (device_id,))
    return cur.rowcount == 1


@router.post("/devices", status_code=201)
def create_device(body: DeviceCreate):
    device_id = "d_" + secrets.token_hex(8)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devices (id, apns_token, platform, last_seen_at) VALUES (%s, %s, %s, now())",
            (device_id, body.apns_token, body.platform))
    return {"device_id": device_id}


@router.patch("/devices")
def update_device(body: DeviceUpdate, x_device_id: str | None = Header(default=None)):
    if not x_device_id:
        return _no_device_header()
    fields = body.model_dump(exclude_unset=True)
    with connect() as conn, conn.cursor() as cur:
        sets = ["last_seen_at = now()"] + [f"{k} = %s" for k in fields]
        cur.execute(
            f"UPDATE devices SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id, notify_on, apns_token IS NOT NULL",
            (*fields.values(), x_device_id))
        row = cur.fetchone()
    if not row:
        return _device_not_found()
    return {"device_id": row[0], "notify_on": row[1], "has_apns_token": row[2]}


@router.get("/me/valleys")
def my_valleys(x_device_id: str | None = Header(default=None)):
    if not x_device_id:
        return _no_device_header()
    sql = ("SELECT v.id, v.name, v.region, v.lat, v.lon FROM valleys v "
           "JOIN subscriptions s ON s.valley_id = v.id "
           "WHERE v.is_active AND s.device_id = %s ORDER BY s.created_at, v.id")
    return _valley_list(sql, (x_device_id,), x_device_id)


def _check(cur, device_id: str, valley_id: int) -> JSONResponse | None:
    if not _touch_device(cur, device_id):
        return _device_not_found()
    cur.execute("SELECT 1 FROM valleys WHERE id = %s AND is_active", (valley_id,))
    if not cur.fetchone():
        return _not_found(valley_id)
    return None


@router.put("/me/subscriptions/{valley_id}")
def subscribe(valley_id: int, x_device_id: str | None = Header(default=None)):
    if not x_device_id:
        return _no_device_header()
    with connect() as conn, conn.cursor() as cur:
        err = _check(cur, x_device_id, valley_id)
        if err:
            return err
        cur.execute(
            "INSERT INTO subscriptions (device_id, valley_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (x_device_id, valley_id))
    return {"valley_id": valley_id, "subscribed": True}


@router.delete("/me/subscriptions/{valley_id}")
def unsubscribe(valley_id: int, x_device_id: str | None = Header(default=None)):
    if not x_device_id:
        return _no_device_header()
    with connect() as conn, conn.cursor() as cur:
        err = _check(cur, x_device_id, valley_id)
        if err:
            return err
        cur.execute("DELETE FROM subscriptions WHERE device_id = %s AND valley_id = %s",
                    (x_device_id, valley_id))
    return {"valley_id": valley_id, "subscribed": False}
