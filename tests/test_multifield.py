"""
Несколько полей на чат + переименование + архив вместо удаления.

Фаза 1 карты OneSoil: «поле как объект». Правила, которые здесь заперты:
id последовательные и никогда не переиспользуются (к id привязан
журнал), архив прячет поле из всех выборок, но историю не трогает,
переименование не задевает ничего, кроме имени.
"""

import asyncio
import time
from datetime import date
from types import SimpleNamespace

import pytest

import bot.main as B
from suv.ledger import Ledger


@pytest.fixture
def led(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "t.db")
    monkeypatch.setattr(B, "LEDGER", led)
    return led


def _mk(led, fid, chat=777, name="Dala"):
    led.upsert_field(
        field_id=fid, name=name, owner_chat_id=chat, hectares=1.0,
        lat=39.5, lon=67.0, elevation_m=700.0, crop_key="cotton",
        soil_key="loam", planting_date="2026-04-10",
        irrigation_method="furrow", water_table_depth_m=0.0)


# ------------------------------------------------------------------- id

def test_ids_are_sequential(led):
    assert B._next_field_id(777) == "TG-777"
    _mk(led, "TG-777")
    assert B._next_field_id(777) == "TG-777-2"
    _mk(led, "TG-777-2")
    assert B._next_field_id(777) == "TG-777-3"


def test_archived_id_is_never_reused(led):
    _mk(led, "TG-777")
    _mk(led, "TG-777-2")
    led.archive_field("TG-777-2")
    # Журнал поля №2 живёт под этим id — новое поле обязано взять №3.
    assert B._next_field_id(777) == "TG-777-3"


def test_agronomist_fields_do_not_block_bot_ids(led):
    _mk(led, "FAR-OLMA", chat=777)
    assert B._next_field_id(777) == "TG-777"


# ---------------------------------------------------------------- архив

def test_archive_hides_field_everywhere_but_keeps_history(led):
    _mk(led, "TG-777", name="Birinchi")
    _mk(led, "TG-777-2", name="Ikkinchi")
    assert len(B._owner_fields(777)) == 2
    assert led.archive_field("TG-777-2") is True
    assert [r["field_id"] for r in B._owner_fields(777)] == ["TG-777"]
    assert [r["field_id"] for r in B._all_fields()] == ["TG-777"]
    # Для id-последовательности архив ВИДЕН.
    assert len(B._owner_fields(777, include_archived=True)) == 2
    # Строка не удалена: _field_row достаёт её по id (история жива).
    row = B._field_row("TG-777-2")
    assert row is not None and row["archived_at"] is not None


def test_archive_is_one_shot(led):
    _mk(led, "TG-777")
    assert led.archive_field("TG-777") is True
    assert led.archive_field("TG-777") is False


def test_push_skips_archived_owners(led):
    """Утренний обход не будит владельца, у которого всё в архиве."""
    import sqlite3
    _mk(led, "TG-777")
    led.archive_field("TG-777")
    with sqlite3.connect(led.path) as c:
        owners = [r[0] for r in c.execute(
            "SELECT DISTINCT owner_chat_id FROM fields "
            "WHERE owner_chat_id IS NOT NULL AND archived_at IS NULL")]
    assert owners == []


# ---------------------------------------------------------- переименование

def test_rename_changes_name_only(led):
    _mk(led, "TG-777", name="Mening dalam")
    assert led.rename_field("TG-777", "Olma bog'i") is True
    row = B._field_row("TG-777")
    assert row["name"] == "Olma bog'i"
    assert row["hectares"] == 1.0 and row["crop_key"] == "cotton"


def test_rename_of_missing_field_is_false(led):
    assert led.rename_field("NOPE", "x") is False


def test_max_fields_cap_constant():
    assert B.MAX_FIELDS_PER_CHAT == 10


# ------------------------------- срок жизни выбора поля в /nom
#
# Асинхронных тестов в пакете нет, и плагина под них тоже: хендлеры
# крутим через asyncio.run, чтобы не тащить зависимость ради двух
# проверок.

class _Msg:
    def __init__(self, text):
        self.text, self.message_id = text, 1
        self.sent: list[str] = []

    async def reply_text(self, t, **kw):
        self.sent.append(t)


class _Upd:
    def __init__(self, text, chat=777):
        self.message = _Msg(text)
        self.effective_chat = SimpleNamespace(id=chat)


def _ctx(**user_data):
    return SimpleNamespace(user_data=dict(user_data), bot=None)


def test_stale_rename_pick_does_not_eat_the_next_question(led):
    """Вопрос фермера не должен превращаться в имя поля.

    При двух и более полях /nom выходит из ConversationHandler и ждёт
    текст по флагу `rename_fid` в user_data. 12.09.2026 флаг жил там
    бессрочно: фермер выбирал поле кнопкой, уходил, а следующим
    свободным текстом — вопросом Амиру — переименовывал поле. Сам
    вопрос до Амира не доходил: savol возвращался раньше пересылки.
    """
    _mk(led, "TG-777", name="Paxta dalasi")
    ctx = _ctx(rename_fid="TG-777", rename_until=0.0)   # срок вышел
    upd = _Upd("Amir, uzumga qachon dori sepaman?")

    eaten = asyncio.run(B.rename_text_free(upd, ctx))

    assert eaten is False, "просроченный выбор поля съел вопрос"
    assert B._field_row("TG-777")["name"] == "Paxta dalasi"
    assert ctx.user_data.get("rename_fid") is None


def test_fresh_rename_pick_still_renames(led):
    """Обратная сторона того же замка: в пределах срока имя меняется."""
    _mk(led, "TG-777", name="Paxta dalasi")
    ctx = _ctx(rename_fid="TG-777",
               rename_until=time.time() + B.RENAME_TTL_SEC)

    assert asyncio.run(B.rename_text_free(_Upd("Uzumzor"), ctx)) is True
    assert B._field_row("TG-777")["name"] == "Uzumzor"
    assert ctx.user_data.get("rename_until") is None
