"""Проверка подписи Telegram — единственный замок кабинета фермера.

Здесь нет «проверим, что хорошее проходит» без обратного: каждый тест
подделки описывает конкретную атаку, от которой защищаемся.
"""

import hashlib
import hmac
import json
import urllib.parse

import pytest

from suv.webauth import (AuthError, verify_init_data, verify_login_widget)

TOKEN = "123456:TEST-TOKEN-not-a-real-one"
NOW = 1_760_000_000.0
USER = {"id": 43348525, "first_name": "Farrux", "last_name": "Urunov",
        "username": "farrux", "language_code": "uz"}


def _sign_init_data(fields: dict, token: str = TOKEN) -> str:
    """Собрать initData так, как это делает Telegram."""
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": h})


def _sign_widget(fields: dict, token: str = TOKEN) -> dict:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hashlib.sha256(token.encode()).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {**fields, "hash": h}


def _init_data(**over) -> str:
    fields = {"user": json.dumps(USER, separators=(",", ":")),
              "auth_date": str(int(NOW) - 60), "query_id": "AAH"}
    fields.update(over)
    return _sign_init_data(fields)


# ── честный вход ───────────────────────────────────────────────


def test_valid_init_data_returns_user():
    u = verify_init_data(_init_data(), TOKEN, now=NOW)
    assert u.id == 43348525
    assert u.display_name == "Farrux Urunov"
    assert u.language_code == "uz"


def test_valid_login_widget_returns_user():
    data = _sign_widget({**{k: str(v) for k, v in USER.items()},
                         "auth_date": str(int(NOW) - 60)})
    u = verify_login_widget(data, TOKEN, now=NOW)
    assert u.id == 43348525
    assert u.display_name == "Farrux Urunov"


def test_display_name_falls_back_to_username_then_id():
    d = _init_data(user=json.dumps({"id": 7, "username": "hero"}))
    assert verify_init_data(d, TOKEN, now=NOW).display_name == "hero"
    d = _init_data(user=json.dumps({"id": 7}))
    assert verify_init_data(d, TOKEN, now=NOW).display_name == "7"


# ── подделки ───────────────────────────────────────────────────


def test_tampered_user_id_rejected():
    """Главная атака: подменить id и зайти в чужое поле."""
    good = _init_data()
    evil = good.replace(urllib.parse.quote(str(USER["id"])), "999")
    assert evil != good
    with pytest.raises(AuthError):
        verify_init_data(evil, TOKEN, now=NOW)


def test_signature_from_other_bot_rejected():
    fields = {"user": json.dumps(USER), "auth_date": str(int(NOW) - 60)}
    foreign = _sign_init_data(fields, token="999:SOMEONE-ELSES-BOT")
    with pytest.raises(AuthError):
        verify_init_data(foreign, TOKEN, now=NOW)


def test_widget_signature_not_accepted_as_miniapp():
    """Два канала подписываются разными ключами и не взаимозаменяемы."""
    fields = {**{k: str(v) for k, v in USER.items()},
              "auth_date": str(int(NOW) - 60)}
    widget = _sign_widget(fields)
    as_query = urllib.parse.urlencode(widget)
    with pytest.raises(AuthError):
        verify_init_data(as_query, TOKEN, now=NOW)


def test_miniapp_signature_not_accepted_as_widget():
    raw = _init_data()
    as_dict = dict(urllib.parse.parse_qsl(raw))
    with pytest.raises(AuthError):
        verify_login_widget(as_dict, TOKEN, now=NOW)


def test_missing_hash_rejected():
    with pytest.raises(AuthError):
        verify_init_data("user=%7B%22id%22%3A1%7D&auth_date=1", TOKEN, now=NOW)


def test_empty_and_none_token_rejected():
    for token in ("", None):
        with pytest.raises(AuthError):
            verify_init_data(_init_data(), token or "", now=NOW)


def test_stale_data_rejected():
    old = _init_data(auth_date=str(int(NOW) - 25 * 3600))
    with pytest.raises(AuthError):
        verify_init_data(old, TOKEN, now=NOW)


def test_fresh_data_within_window_accepted():
    ok = _init_data(auth_date=str(int(NOW) - 23 * 3600))
    assert verify_init_data(ok, TOKEN, now=NOW).id == 43348525


def test_future_auth_date_rejected():
    """Подпись «из будущего» жила бы дольше окна — это обход срока."""
    future = _init_data(auth_date=str(int(NOW) + 3600))
    with pytest.raises(AuthError):
        verify_init_data(future, TOKEN, now=NOW)


def test_small_clock_skew_tolerated():
    skewed = _init_data(auth_date=str(int(NOW) + 20))
    assert verify_init_data(skewed, TOKEN, now=NOW).id == 43348525


def test_missing_auth_date_rejected():
    fields = {"user": json.dumps(USER)}
    with pytest.raises(AuthError):
        verify_init_data(_sign_init_data(fields), TOKEN, now=NOW)


def test_broken_user_json_rejected():
    with pytest.raises(AuthError):
        verify_init_data(_init_data(user="{не json"), TOKEN, now=NOW)


def test_user_without_id_rejected():
    d = _init_data(user=json.dumps({"first_name": "Аноним"}))
    with pytest.raises(AuthError):
        verify_init_data(d, TOKEN, now=NOW)


def test_empty_init_data_rejected():
    with pytest.raises(AuthError):
        verify_init_data("", TOKEN, now=NOW)
