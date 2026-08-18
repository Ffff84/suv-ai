"""Кабинет фермера: доступ к чужому полю и совпадение цифр с ботом.

Главная проверка здесь не «ручка отвечает 200», а «чужое поле не
отдаётся». Кабинет открыт в интернет, и подпись Telegram — единственное,
что отделяет фермера от чужих данных.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import urllib.parse

import pytest

TOKEN = "123456:TEST-TOKEN-not-a-real-one"
FARMER = 43348525
STRANGER = 99999999


def _init_data(uid: int, token: str = TOKEN) -> str:
    import time
    fields = {"user": json.dumps({"id": uid, "first_name": "T",
                                  "language_code": "ru"},
                                 separators=(",", ":")),
              "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": h})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Свежая база с одним полем фермера и приложение поверх неё."""
    db = tmp_path / "suv.db"
    monkeypatch.setenv("SUV_DB", str(db))
    monkeypatch.setenv("TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "")
    monkeypatch.setenv("OBSERVER_CHAT_IDS", "")

    from suv.ledger import Ledger
    Ledger(str(db))                      # создаёт схему
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO fields (field_id, name, owner_chat_id, hectares, "
            "lat, lon, elevation_m, crop_key, soil_key, planting_date, "
            "irrigation_method, water_table_depth_m, baseline_interval_days, "
            "created_at) "
            "VALUES ('FAR-TEST','Testzor',?,2.0,39.55,66.99,700,'apple',"
            "'sandy_loam','2018-03-20','drip',40.0,30,'2026-08-01')",
            (FARMER,))

    # Модули читают SUV_DB на импорте — гарантируем свежий импорт.
    for mod in ("bot.main", "web.app"):
        import sys
        sys.modules.pop(mod, None)

    from fastapi.testclient import TestClient
    import web.app as W
    return TestClient(W.app)


def _auth(uid: int) -> dict:
    return {"Authorization": "tma " + _init_data(uid)}


# ── доступ ─────────────────────────────────────────────────────


def test_no_header_rejected(client):
    assert client.get("/api/me").status_code == 401


def test_garbage_signature_rejected(client):
    bad = {"Authorization": "tma user=%7B%22id%22%3A1%7D&hash=deadbeef"}
    assert client.get("/api/me", headers=bad).status_code == 401


def test_signature_from_other_bot_rejected(client):
    forged = {"Authorization": "tma " + _init_data(FARMER, "999:OTHER-BOT")}
    assert client.get("/api/me", headers=forged).status_code == 401


def test_farmer_sees_own_field(client):
    r = client.get("/api/me", headers=_auth(FARMER))
    assert r.status_code == 200
    ids = [f["field_id"] for f in r.json()["fields"]]
    assert ids == ["FAR-TEST"]


def test_stranger_sees_no_fields(client):
    r = client.get("/api/me", headers=_auth(STRANGER))
    assert r.status_code == 200
    assert r.json()["fields"] == []


def test_stranger_cannot_open_someone_elses_field(client):
    """Ключевой тест: знание field_id не даёт доступа к полю."""
    r = client.get("/api/field/FAR-TEST", headers=_auth(STRANGER))
    assert r.status_code == 404


def test_field_id_pattern_blocks_traversal(client):
    r = client.get("/api/field/..%2F..%2Fetc", headers=_auth(FARMER))
    assert r.status_code in (404, 422)


def test_health_open_and_has_no_data(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "fields" not in r.json()
