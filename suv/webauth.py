"""Подтверждение личности фермера через Telegram — без логинов и паролей.

Фермеру нечего запоминать: Telegram уже знает, кто он, и подписывает это
криптографически. Два входа, оба сводятся к одной проверке подписи.

**Mini App** (`initData`). Фермер жмёт кнопку в боте, открывается веб-вид,
и Telegram кладёт в страницу строку с данными пользователя и подписью.
Ключ подписи: HMAC(key="WebAppData", msg=токен_бота).

**Login Widget** (кнопка «Войти через Telegram» на сайте). Тот же набор
полей, но ключ другой: SHA256(токен_бота). Перепутать их нельзя — подпись
не сойдётся, и это правильно: два разных канала не должны подделываться
один под другой.

Почему это надёжно. Подпись считается на стороне Telegram токеном бота,
который знаем только мы и Telegram. Подделать `user.id` не выйдет: любая
правка данных ломает хеш. Единственный секрет — токен бота; он и так
охраняется, потому что даёт полный контроль над ботом.

Чего модуль НЕ делает и не должен: не ходит в сеть, не читает базу, не
знает про поля. Только «эта строка подписана нашим ботом и не протухла».
Так его можно проверить тестами до последней ветки.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

# Сутки. Telegram рекомендует ограничивать возраст initData, иначе
# перехваченная строка работает вечно. Сутки — компромисс: фермер,
# открывший вкладку утром, доработает день без перелогина.
MAX_AGE_S = 24 * 60 * 60


class AuthError(Exception):
    """Данные не подписаны нашим ботом, протухли или испорчены."""


@dataclass(frozen=True)
class TelegramUser:
    """Кто пришёл. Ровно то, что подписал Telegram, без домыслов."""

    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language_code: str = ""

    @property
    def display_name(self) -> str:
        full = " ".join(p for p in (self.first_name, self.last_name) if p)
        return full or self.username or str(self.id)


def _check_string(pairs: list[tuple[str, str]]) -> str:
    """Каноническая строка проверки: пары key=value, отсортированы, без hash."""
    return "\n".join(f"{k}={v}" for k, v in sorted(pairs) if k != "hash")


def _verify(pairs: list[tuple[str, str]], secret: bytes, given: str) -> bool:
    mine = hmac.new(secret, _check_string(pairs).encode(), hashlib.sha256)
    # compare_digest, а не ==: обычное сравнение строк выходит раньше на
    # первом несовпавшем байте и по времени ответа выдаёт, сколько знаков
    # угадано. На подписи это реальный вектор подбора.
    return hmac.compare_digest(mine.hexdigest(), given)


def _fresh(pairs: list[tuple[str, str]], now: float, max_age_s: int) -> None:
    raw = dict(pairs).get("auth_date")
    if not raw:
        raise AuthError("нет auth_date")
    try:
        issued = int(raw)
    except ValueError as exc:
        raise AuthError("auth_date не число") from exc
    age = now - issued
    if age > max_age_s:
        raise AuthError(f"данные устарели на {int(age - max_age_s)} с")
    # Часы сервера и Telegram могут разойтись на секунды; отрицательный
    # возраст в пределах минуты — это рассинхрон, а не подделка. Больше —
    # уже подозрительно, потому что подпись из будущего живёт дольше суток.
    if age < -60:
        raise AuthError("auth_date из будущего")


def _user_from(pairs: list[tuple[str, str]]) -> TelegramUser:
    data = dict(pairs)
    raw = data.get("user")
    if raw:                                   # Mini App: JSON в поле user
        try:
            u = json.loads(raw)
        except ValueError as exc:
            raise AuthError("поле user не читается как JSON") from exc
    elif data.get("id"):                      # Login Widget: поля плоские
        u = data
    else:
        raise AuthError("в данных нет пользователя")
    try:
        uid = int(u["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthError("нет числового id пользователя") from exc
    return TelegramUser(
        id=uid,
        first_name=str(u.get("first_name") or ""),
        last_name=str(u.get("last_name") or ""),
        username=str(u.get("username") or ""),
        language_code=str(u.get("language_code") or ""),
    )


def verify_init_data(init_data: str, bot_token: str, *,
                     now: float | None = None,
                     max_age_s: int = MAX_AGE_S) -> TelegramUser:
    """Проверить `initData` из Mini App и вернуть, кто это.

    Бросает AuthError на любой сомнительный вход — вызывающему остаётся
    отдать 401, а не разбираться в причинах.
    """
    if not bot_token:
        raise AuthError("нет токена бота — проверять нечем")
    if not init_data:
        raise AuthError("пустой initData")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    given = dict(pairs).get("hash", "")
    if not given:
        raise AuthError("нет подписи")
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    if not _verify(pairs, secret, given):
        raise AuthError("подпись не сходится")
    _fresh(pairs, time.time() if now is None else now, max_age_s)
    return _user_from(pairs)


def verify_login_widget(data: dict, bot_token: str, *,
                        now: float | None = None,
                        max_age_s: int = MAX_AGE_S) -> TelegramUser:
    """Проверить данные кнопки «Войти через Telegram» на обычном сайте.

    Отличие от Mini App ровно одно и оно принципиальное: секрет здесь —
    SHA256 токена, а не HMAC с меткой WebAppData.
    """
    if not bot_token:
        raise AuthError("нет токена бота — проверять нечем")
    given = str(data.get("hash") or "")
    if not given:
        raise AuthError("нет подписи")
    pairs = [(k, str(v)) for k, v in data.items()]
    secret = hashlib.sha256(bot_token.encode()).digest()
    if not _verify(pairs, secret, given):
        raise AuthError("подпись не сходится")
    _fresh(pairs, time.time() if now is None else now, max_age_s)
    return _user_from(pairs)
