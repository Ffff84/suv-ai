"""
Гейт закрытого демо «Dala holati».

Тест существует из-за конкретной поломки: кнопка демо попала в общий
список кнопок меню, и текст «🌾 Dala holati» перестал быть обычным
ответом на вопрос мастера регистрации. Для чата вне демо это означало
молча оборванную регистрацию — ровно та тишина, которой бот не должен
отвечать никогда.

Правило: вне FIELD_STATUS_CHAT_IDS этот текст ведёт себя ровно так же,
как до появления экрана.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest
from telegram import Chat, Message, Update, User

FARMER = 555          # обычный фермер, демо ему не выдано
DEMO = 777            # chat_id из FIELD_STATUS_CHAT_IDS


@pytest.fixture(autouse=True)
def _restore_bot_module():
    """Тесты перечитывают модуль с разным окружением; вернуть его в
    исходное состояние, чтобы соседние файлы видели обычного бота."""
    yield
    import bot.main
    importlib.reload(bot.main)


def _reload_bot(monkeypatch, tmp_path, demo_ids: str):
    """Перечитать бота с заданным списком демо-чатов: списки читаются
    из окружения один раз, на импорте."""
    monkeypatch.setenv("FIELD_STATUS_CHAT_IDS", demo_ids)
    monkeypatch.setenv("SUV_DB", str(tmp_path / "gate.db"))
    monkeypatch.setenv("TELEGRAM_TOKEN", "test:token")
    import bot.main as m
    return importlib.reload(m)


def _text_update(chat_id: int, text: str) -> Update:
    user = User(id=chat_id, is_bot=False, first_name="Fermer")
    msg = Message(message_id=1, date=datetime.now(timezone.utc),
                  chat=Chat(id=chat_id, type=Chat.PRIVATE),
                  from_user=user, text=text)
    return Update(update_id=1, message=msg)


def test_dala_button_is_not_a_menu_button_for_others(monkeypatch, tmp_path):
    """Пустой список демо: текст кнопки — обычный ответ мастера.

    MENU_FILTER вычитается из вопросов регистрации (not_menu). Если он
    совпадёт, мастер пропустит ответ и упадёт в fallback.
    """
    m = _reload_bot(monkeypatch, tmp_path, "")
    upd = _text_update(FARMER, m.BTN_DALA)
    assert not m.MENU_FILTER.check_update(upd)
    assert not m.DALA_FILTER.check_update(upd)
    # Прежние кнопки меню продолжают вычитаться, как и раньше.
    assert m.MENU_FILTER.check_update(_text_update(FARMER, m.BTN_SUV))


def test_dala_button_works_only_for_demo_chats(monkeypatch, tmp_path):
    m = _reload_bot(monkeypatch, tmp_path, str(DEMO))
    assert m.DALA_FILTER.check_update(_text_update(DEMO, m.BTN_DALA))
    assert m.MENU_FILTER.check_update(_text_update(DEMO, m.BTN_DALA))
    # Фермер вне демо не задет, даже когда демо кому-то выдано.
    assert not m.DALA_FILTER.check_update(_text_update(FARMER, m.BTN_DALA))
    assert not m.MENU_FILTER.check_update(_text_update(FARMER, m.BTN_DALA))


def test_menu_keyboard_grows_only_for_demo_chats(monkeypatch, tmp_path):
    m = _reload_bot(monkeypatch, tmp_path, str(DEMO))
    assert m._menu(FARMER) is m.MAIN_MENU
    assert m._menu(DEMO) is m.MAIN_MENU_DALA
    assert len(m.MAIN_MENU.keyboard) == 2
