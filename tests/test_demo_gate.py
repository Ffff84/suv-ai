"""
Гейт закрытого демо «Dala holati».

Тест существует из-за конкретной поломки: кнопка демо попала в общий
список кнопок меню, и текст «🌾 Dala holati» перестал быть обычным
ответом на вопрос мастера регистрации. Для чата вне демо это означало
молча оборванную регистрацию — ровно та тишина, которой бот не должен
отвечать никогда.

Правило: вне FIELD_STATUS_CHAT_IDS этот текст ведёт себя ровно так же,
как до появления экрана.

09.09.2026 смысл пустого списка ПЕРЕВЁРНУТ: пусто = открыто всем, как у
ALLOWED_CHAT_IDS. Прежнее «пусто = ни у кого» молча выключало главную
функцию продукта — без обведённого контура NDVI считается по квадрату
200x200 м вместе с дорогой и соседским полем. Гарантия про мастер
регистрации при этом не отменяется: она переехала в тест про НЕпустой
список, где чат вне демо действительно существует.
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


def test_empty_list_opens_the_screen_to_everyone(monkeypatch, tmp_path):
    """Пустой список = открыто всем.

    Это ровно то правило, по которому уже живёт ALLOWED_CHAT_IDS. Пока
    оно было обратным, обводка контура была недоступна никому, кроме
    трёх демо-чатов, — то есть «карта влаги бесплатно навсегда» из
    PRODUCT.md не работала ни у одного пришедшего сам фермера.
    """
    m = _reload_bot(monkeypatch, tmp_path, "")
    assert m._field_status_open(FARMER)
    upd = _text_update(FARMER, m.BTN_DALA)
    assert m.DALA_FILTER.check_update(upd)
    assert m.MENU_FILTER.check_update(upd)
    assert m.MENU_FILTER.check_update(_text_update(FARMER, m.BTN_SUV))


def test_non_empty_list_still_closes_the_screen_to_others(monkeypatch, tmp_path):
    """Обратный ход есть и он один: вписать chat_id в переменную.

    И для чата вне списка текст кнопки обязан остаться обычным ответом
    мастера: MENU_FILTER вычитается из вопросов регистрации (not_menu),
    и если он совпадёт, мастер пропустит ответ и упадёт в fallback —
    ровно та тишина, которой бот не отвечает никогда.
    """
    m = _reload_bot(monkeypatch, tmp_path, str(DEMO))
    assert m._field_status_open(DEMO)
    assert not m._field_status_open(FARMER)
    upd = _text_update(FARMER, m.BTN_DALA)
    assert not m.DALA_FILTER.check_update(upd)
    assert not m.MENU_FILTER.check_update(upd)
    assert m.MENU_FILTER.check_update(_text_update(FARMER, m.BTN_SUV))


def test_dala_button_works_only_for_demo_chats(monkeypatch, tmp_path):
    m = _reload_bot(monkeypatch, tmp_path, str(DEMO))
    assert m.DALA_FILTER.check_update(_text_update(DEMO, m.BTN_DALA))
    assert m.MENU_FILTER.check_update(_text_update(DEMO, m.BTN_DALA))
    # Фермер вне демо не задет, даже когда демо кому-то выдано.
    assert not m.DALA_FILTER.check_update(_text_update(FARMER, m.BTN_DALA))
    assert not m.MENU_FILTER.check_update(_text_update(FARMER, m.BTN_DALA))


def _labels(markup):
    """Подписи кнопок клавиатуры — и строк, и WebApp-кнопок."""
    return [[b if isinstance(b, str) else b.text for b in row]
            for row in markup.keyboard]


def test_menu_keyboard_grows_only_for_demo_chats(monkeypatch, tmp_path):
    m = _reload_bot(monkeypatch, tmp_path, str(DEMO))
    # Проверяем состав меню, а не тождество с константой: клавиатура
    # теперь собирается по флагам, и смысл теста — «лишнего у фермера
    # не появилось», а не «вернулся тот же объект».
    assert _labels(m._menu(FARMER)) == [[m.BTN_SUV, m.BTN_BAJARDIM],
                                        [m.BTN_TEJALDI, m.BTN_YORDAM]]
    assert m.BTN_DALA in _labels(m._menu(DEMO))[-1]
    assert len(m.MAIN_MENU.keyboard) == 2


def _give_field(m, chat: int) -> None:
    """Завести чату поле: кабинет — разбор ПОЛЯ, и без поля кнопки нет."""
    m.LEDGER.upsert_field(
        field_id=f"TG-{chat}", name="Dala", owner_chat_id=chat, hectares=1.0,
        lat=39.5, lon=67.0, elevation_m=700.0, crop_key="cotton",
        soil_key="loam", planting_date="2026-04-10",
        irrigation_method="furrow", water_table_depth_m=0.0)


def test_cabinet_button_only_for_its_own_gate(monkeypatch, tmp_path):
    """Кабинет за своим гейтом: демо «Dala holati» его не открывает."""
    m = _reload_bot(monkeypatch, tmp_path, str(DEMO))
    monkeypatch.setattr(m, "CABINET_URL", "https://suv-ai.online/dala/")
    monkeypatch.setattr(m, "_CABINET", {DEMO})
    _give_field(m, DEMO)
    _give_field(m, FARMER)
    assert m.BTN_KABINET in _labels(m._menu(DEMO))[-1]
    assert m.BTN_KABINET not in sum(_labels(m._menu(FARMER)), [])


def test_empty_cabinet_list_opens_it_to_everyone_with_a_field(monkeypatch,
                                                              tmp_path):
    """Третий гейт выровнен по двум соседним: пусто = открыто всем.

    До 12.09.2026 здесь было обратное правило, и пустая переменная
    закрывала кабинет ВСЕМ, включая Амира: единственным признаком была
    отсутствующая кнопка.
    """
    m = _reload_bot(monkeypatch, tmp_path, "")
    monkeypatch.setattr(m, "CABINET_URL", "https://suv-ai.online/dala/")
    monkeypatch.setattr(m, "_CABINET", set())
    _give_field(m, FARMER)
    assert m.BTN_KABINET in sum(_labels(m._menu(FARMER)), [])


def test_cabinet_button_is_not_a_door_to_an_empty_room(monkeypatch, tmp_path):
    """Открытый список не означает кнопку у каждого встречного.

    Бот открыт жюри, а web/app.py про список кабинета не знает вовсе:
    пускает по подписи Telegram и отдаёт поля через _fields_for_view,
    то есть постороннему — ни одного. Кнопка у того, за кем не записано
    ни одного поля, вела бы на пустой экран.
    """
    m = _reload_bot(monkeypatch, tmp_path, "")
    monkeypatch.setattr(m, "CABINET_URL", "https://suv-ai.online/dala/")
    monkeypatch.setattr(m, "_CABINET", set())
    assert m._cabinet_open(FARMER) is False
    assert m.BTN_KABINET not in sum(_labels(m._menu(FARMER)), [])


def test_cabinet_button_hidden_without_url(monkeypatch, tmp_path):
    """Без домена кнопки нет: web_app без https Telegram не откроет."""
    m = _reload_bot(monkeypatch, tmp_path, str(DEMO))
    monkeypatch.setattr(m, "CABINET_URL", "")
    monkeypatch.setattr(m, "_CABINET", {DEMO})
    assert m.BTN_KABINET not in sum(_labels(m._menu(DEMO)), [])




# ------------------------------------------------- третий гейт: кабинет
#
# _CABINET жил по ОБРАТНОЙ конвенции: проверка писалась по месту,
# `chat_id in _CABINET`, без оговорки про пустоту. Пустая переменная
# закрывала кабинет всем, включая Амира, — при том что та же пустота у
# _authorized и _field_status_open значит «открыто всем». Два смысла
# пустоты в трёх списках одного файла — ошибка не «если», а «когда».


def test_all_three_gates_read_an_empty_list_the_same_way(monkeypatch, tmp_path):
    """Пусто = открыто всем. Одинаково у всех трёх списков."""
    m = _reload_bot(monkeypatch, tmp_path, "")
    monkeypatch.setattr(m, "_ALLOWED", set())
    monkeypatch.setattr(m, "_CABINET", set())
    assert m._authorized(_text_update(FARMER, m.BTN_SUV))
    assert m._field_status_open(FARMER)
    # У кабинета к общей конвенции добавлено второе условие: показывать
    # его тому, за кем записано поле. Пустой список сам по себе больше
    # не закрывает — закрывает отсутствие поля.
    _give_field(m, FARMER)
    assert m._cabinet_open(FARMER)


def test_empty_cabinet_list_shows_the_button_to_everyone(monkeypatch, tmp_path):
    """Кнопка кабинета при пустом списке видна всем — но только с URL.

    Обратное правило раньше означало: переменную забыли заполнить —
    кабинета нет ни у кого, и понять это можно было лишь по
    отсутствующей кнопке, без единой строки в логе.
    """
    m = _reload_bot(monkeypatch, tmp_path, "")
    monkeypatch.setattr(m, "_CABINET", set())
    monkeypatch.setattr(m, "CABINET_URL", "https://suv-ai.online/dala/")
    _give_field(m, FARMER)
    assert m.BTN_KABINET in _labels(m._menu(FARMER))[-1]
    monkeypatch.setattr(m, "CABINET_URL", "")
    assert m.BTN_KABINET not in sum(_labels(m._menu(FARMER)), [])


# ------------------------------------------ файл границ за тем же гейтом


def _doc_update(chat_id: int, name: str = "granitsalar.kml") -> Update:
    from telegram import Document
    user = User(id=chat_id, is_bot=False, first_name="Fermer")
    doc = Document(file_id="f1", file_unique_id="u1", file_name=name)
    msg = Message(message_id=2, date=datetime.now(timezone.utc),
                  chat=Chat(id=chat_id, type=Chat.PRIVATE),
                  from_user=user, document=doc)
    return Update(update_id=2, message=msg)


def _registered_app(monkeypatch, m):
    """Поднять НАСТОЯЩУЮ регистрацию хендлеров, не доходя до сети."""
    from telegram.ext import Application
    box = {}

    def _stop(self, *a, **kw):
        box["app"] = self

    monkeypatch.setattr(Application, "run_polling", _stop)
    m.main()
    return box["app"]


def test_file_import_is_gated_like_the_photo_note(monkeypatch, tmp_path):
    """Файл границ закрыт тем же гейтом, что фотозаметка.

    Регистрация filters.Document.ALL стояла БЕЗ гейта — в отличие от
    соседней строки с filters.PHOTO. При снятом allowlist (а он снят с
    11.08.2026, бот открыт жюри) это значило: любой чат присылает KML и
    сеет поля в живой журнал, из которого считается акт экономии.

    Проверяем не строку в исходнике, а саму регистрацию: глазами эту
    строку уже пропустили один раз.
    """
    m = _reload_bot(monkeypatch, tmp_path, str(DEMO))
    app = _registered_app(monkeypatch, m)

    def _takes(chat_id: int) -> bool:
        return any(getattr(h, "callback", None) is m.import_doc
                   and bool(h.check_update(_doc_update(chat_id)))
                   for h in app.handlers[0])

    assert _takes(DEMO), "импорт не дошёл даже до демо-чата"
    assert not _takes(FARMER), "чат вне демо сеет поля файлом"


def test_open_gate_leaves_file_import_open_to_everyone(monkeypatch, tmp_path):
    """Пустой список — импорт работает у всех, как и всё остальное.

    Гейт добавлен ради закрытия, а не ради того, чтобы выключить
    пакетный посев: на сервере список пуст, и поведение импорта этой
    правкой не меняется ни на шаг.
    """
    m = _reload_bot(monkeypatch, tmp_path, "")
    app = _registered_app(monkeypatch, m)
    assert any(getattr(h, "callback", None) is m.import_doc
               and bool(h.check_update(_doc_update(FARMER)))
               for h in app.handlers[0])
