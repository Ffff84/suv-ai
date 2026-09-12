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




# ------------------------------- потолок полей на чат на пути импорта
#
# Мастер на MAX_FIELDS_PER_CHAT останавливался, а файл границ заводил
# столько полей, сколько их в KML: предохранитель против случайного
# зоопарка работал ровно на одном входе из двух. 25 полей в один чат
# ломают не только /tejaldi — это ещё и 25 утренних сообщений.


class _ImportMsg:
    def __init__(self, file_name="granitsalar.geojson"):
        self.sent: list[str] = []
        self.document = SimpleNamespace(
            file_name=file_name, file_size=1000, get_file=self._get_file)

    async def _get_file(self):
        async def _download():
            return bytearray(b"{}")
        return SimpleNamespace(download_as_bytearray=_download)

    async def reply_text(self, t, **kw):
        self.sent.append(t)


def _import_update(chat=777):
    msg = _ImportMsg()
    return SimpleNamespace(message=msg,
                           effective_chat=SimpleNamespace(id=chat)), msg


def _parsed(n):
    from suv.boundary_import import ImportedField
    ring = [(39.5, 67.0), (39.501, 67.0), (39.501, 67.001),
            (39.5, 67.001), (39.5, 67.0)]
    return [ImportedField(name=f"Dala {i}", ring=ring, area_ha=1.0,
                          lat=39.5, lon=67.0) for i in range(n)]


def test_import_trims_the_file_to_the_per_chat_ceiling(led, monkeypatch):
    """Из файла берём столько полей, сколько влезает, и говорим об этом."""
    monkeypatch.setattr(B, "parse_boundaries",
                        lambda data, name: (_parsed(B.MAX_FIELDS_PER_CHAT + 5),
                                            []))
    upd, msg = _import_update()
    ctx = _ctx()
    asyncio.run(B.import_doc(upd, ctx))

    pend = ctx.user_data["pending_import"]
    assert len(pend["fields"]) == B.MAX_FIELDS_PER_CHAT
    assert str(B.MAX_FIELDS_PER_CHAT) in msg.sent[-1], (
        "потолок молча срезал поля, не сказав фермеру")


def test_import_refuses_when_the_chat_is_already_full(led, monkeypatch):
    """Чат добрал потолок мастером — файл не досеивает сверх него."""
    for i in range(B.MAX_FIELDS_PER_CHAT):
        _mk(led, f"TG-777-{i}")
    monkeypatch.setattr(B, "parse_boundaries",
                        lambda data, name: (_parsed(3), []))
    upd, msg = _import_update()
    ctx = _ctx()
    asyncio.run(B.import_doc(upd, ctx))

    assert "pending_import" not in ctx.user_data
    assert msg.sent, "отказ молча — худший из ответов"
    assert len(B._owner_fields(777)) == B.MAX_FIELDS_PER_CHAT




# ------------------------------- наблюдатель и свои поля
#
# Импорт границ сеет поля с owner_chat сеющего. Показ импорта в чате
# наблюдателя (Амир на сцене) раньше молча снимал с него роль:
# _fields_for_view сначала спрашивал «есть ли свои поля», и после
# импорта отвечал «есть» — /suv, «Tejaldi» и «Dala holati» в том же
# чате переходили на узбекский и показывали импортированные пустышки
# вместо полей Фарруха. До конца выступления: откат — /ochirish по
# два тапа на каждое поле.

OBSERVER = 43348525
FARRUKH = 555111


@pytest.fixture
def observer(monkeypatch):
    """Чат наблюдателя. Списки читаются из окружения на импорте —
    в тесте подменяем сами множества."""
    monkeypatch.setattr(B, "_OBSERVERS", {OBSERVER})
    monkeypatch.setattr(B, "_ALLOWED", set())
    return OBSERVER


def test_observer_keeps_the_role_after_seeding_own_fields(led, observer):
    """Роль чата решается РАНЬШЕ, чем наличие своих полей."""
    _mk(led, "FAR-UZUM", chat=FARRUKH, name="Uzumzor")
    assert B._fields_for_view(observer)[1] is True

    _mk(led, "TG-43348525", chat=observer, name="Klin 1")   # импорт

    rows, is_obs = B._fields_for_view(observer)
    assert is_obs is True, "свои поля сняли режим наблюдателя"
    # Чужие поля не пропали, свои добавились — и язык остаётся русским.
    assert [r["field_id"] for r in rows] == ["FAR-UZUM", "TG-43348525"]


def test_farmer_with_own_fields_is_not_an_observer(led, observer):
    """Обратная сторона замка: Фарруха в _OBSERVERS нет, он видит
    только своё и отвечает по-узбекски."""
    _mk(led, "FAR-UZUM", chat=FARRUKH, name="Uzumzor")
    _mk(led, "TG-43348525", chat=observer, name="Klin 1")
    rows, is_obs = B._fields_for_view(FARRUKH)
    assert is_obs is False
    assert [r["field_id"] for r in rows] == ["FAR-UZUM"]


def test_observer_logs_only_his_own_fields(led, observer, monkeypatch):
    """Журнал — по хозяину ПОЛЯ, а не по роли чата.

    Чужое поле наблюдатель только смотрит: его любопытство не плодит
    строки recommendations, по которым считается дисциплина фермера.
    Своё — записывает, иначе его же «✅ Suv berdim» остался бы без rid.
    """
    _mk(led, "FAR-UZUM", chat=FARRUKH, name="Uzumzor")
    _mk(led, "TG-43348525", chat=observer, name="Klin 1")

    logged: list[str] = []
    langs: list[str] = []

    def _fake_compute(row, today):
        return SimpleNamespace(
            field=SimpleNamespace(field_id=row["field_id"])), None, True, False

    def _fake_log(rec, version):
        logged.append(rec.field.field_id)
        return len(logged)

    def _fake_msg(rec, pump, lang, **kw):
        langs.append(lang)
        return "ok"

    monkeypatch.setattr(B, "_compute_rec", _fake_compute)
    monkeypatch.setattr(B, "_rec_message", _fake_msg)
    monkeypatch.setattr(B.LEDGER, "log_recommendation", _fake_log)

    ctx = _ctx()
    asyncio.run(B.suv(_Upd("/suv", chat=observer), ctx))

    assert langs == ["ru", "ru"], "наблюдателю отвечаем по-русски"
    assert logged == ["TG-43348525"], "в KPI-журнал попало чужое поле"
    assert list(ctx.user_data["last_rec_ids"]) == ["TG-43348525"]




# ------------------------------- /tejaldi против потолка Telegram
#
# Замер 12.09.2026 на полях с именем «Paxta dalasi N»: одним сообщением
# 30 полей — 3349 символов, 40 — 4469, 50 — 5589 при потолке в 4096.
# Фермер на сороковом поле получал «Kechirasiz, xatolik yuz berdi»
# вместо KPI. Полсотни полей — не выдумка: столько заводит импорт
# границ одним файлом.

def test_tejaldi_splits_by_field_boundary_on_forty_fields(led):
    """Куски влезают в потолок, и ни одно поле не разорвано пополам."""
    for i in range(1, 41):
        _mk(led, "TG-777" if i == 1 else f"TG-777-{i}", name=f"Dala {i}")

    upd = _Upd("")
    asyncio.run(B.tejaldi(upd, _ctx()))

    sent = upd.message.sent
    assert len(sent) > 1, "сорок полей всё ещё уходят одним сообщением"
    assert max(len(t) for t in sent) <= B.TG_TEXT_LIMIT, [len(t) for t in sent]
    for i in range(1, 41):
        assert sum(f"Dala {i}\n" in t for t in sent) == 1, \
            f"поле {i} разорвано между сообщениями или потерялось"


def test_tejaldi_with_one_field_is_still_one_message(led):
    """Обратная сторона: то, что и так помещалось, режут не трогая."""
    _mk(led, "TG-777", name="Paxta dalasi")
    upd = _Upd("")
    asyncio.run(B.tejaldi(upd, _ctx()))
    assert len(upd.message.sent) == 1
    assert "(1/1)" not in upd.message.sent[0], "нумерация одного куска — шум"
    assert upd.message.sent[0].startswith("Paxta dalasi\n")


def test_chunk_blocks_never_drops_an_oversized_block():
    """Блок длиннее потолка уходит отдельно, а не в мусор: обрезать —
    соврать усечённой цифрой, выбросить — молча потерять поле."""
    chunks = B._chunk_blocks(["a" * 100, "b" * 5000, "c" * 100], limit=3500)
    assert len(chunks) == 3
    assert sum(c.count("b") for c in chunks) == 5000


def test_tejaldi_without_any_savings_speaks_instead_of_sending_nothing(
        led, monkeypatch):
    """Пустой текст Telegram отвергает, а тишину фермер читает как
    «бот умер». Сказать нечего — говорим это словами."""
    _mk(led, "TG-777", name="Paxta dalasi")

    def _no_savings(field_id):
        raise KeyError(field_id)

    monkeypatch.setattr(B.LEDGER, "savings", _no_savings)
    upd = _Upd("")
    asyncio.run(B.tejaldi(upd, _ctx()))
    assert len(upd.message.sent) == 1
    assert upd.message.sent[0].strip() != ""
