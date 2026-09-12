"""
Оценка совета одним тапом: 👍/👎 под рекомендацией.

«Rate zones» OneSoil на нашем жанре. Голос — НЕ метрика точности
(эталона нет, и 👍 не делает совет верным): это сырьё для разговора с
фермером и разборов. Один голос на рекомендацию и чат, переголосовать
можно — последнее слово за фермером.
"""

from datetime import date

import pytest

import bot.main as B
from suv.ledger import Ledger


@pytest.fixture
def led(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "t.db")
    monkeypatch.setattr(B, "LEDGER", led)
    led.upsert_field(
        field_id="T-1", name="Olmazor", owner_chat_id=1, hectares=2.0,
        lat=39.5, lon=67.0, elevation_m=700.0, crop_key="apple",
        soil_key="sandy_loam", planting_date="2018-03-20",
        irrigation_method="drip", water_table_depth_m=40.0)
    return led


def _rec(led):
    from dataclasses import dataclass, field as dc_field

    @dataclass
    class _F:
        field_id: str = "T-1"
        ndvi: float | None = None
        ndvi_date: date | None = None

    @dataclass
    class _R:
        field: _F = dc_field(default_factory=_F)
        generated_on: date = date(2026, 9, 11)
        action_day: date | None = None
        gross_mm: float = 0.0
        gross_m3: float = 0.0
        reason_key: str = "soil_still_wet"
        plan: list = dc_field(default_factory=list)

    return led.log_recommendation(_R(), "test")


def test_vote_stored_and_revote_replaces(led):
    rid = _rec(led)
    led.add_feedback(rid, "T-1", chat_id=1, verdict="down")
    assert led.feedback_counts("T-1") == (0, 1)
    led.add_feedback(rid, "T-1", chat_id=1, verdict="up")   # передумал
    assert led.feedback_counts("T-1") == (1, 0)


def test_votes_from_two_chats_both_count(led):
    rid = _rec(led)
    led.add_feedback(rid, "T-1", 1, "up")
    led.add_feedback(rid, "T-1", 2, "up")
    assert led.feedback_counts("T-1") == (2, 0)


def test_unknown_verdict_is_refused(led):
    rid = _rec(led)
    with pytest.raises(ValueError):
        led.add_feedback(rid, "T-1", 1, "meh")


def test_recommendation_field_lookup(led):
    rid = _rec(led)
    assert led.recommendation_field(rid) == "T-1"
    assert led.recommendation_field(999_999) is None


def test_markup_adds_vote_row_only_with_rid(monkeypatch):
    monkeypatch.setattr(B, "_FIELD_STATUS", set())    # гейт снят = открыто всем
    plain = B._why_markup(1, "T-1")
    with_fb = B._why_markup(1, "T-1", rid=42)
    assert len(plain.inline_keyboard) == 1            # только «почему»
    assert len(with_fb.inline_keyboard) == 2
    cbs = [b.callback_data for b in with_fb.inline_keyboard[1]]
    assert cbs == ["fb:up:42", "fb:down:42"]




def test_vote_row_survives_the_closed_field_screen_gate(monkeypatch):
    """Оценка совета не привязана к экрану поля.

    При закрытом гейте «Dala holati» _why_markup возвращал reply-меню
    целиком — и вместе с кнопкой «почему» исчезали 👍/👎. Оценка к
    экрану поля отношения не имеет: это единственный канал обратной
    связи по совету, и закрыт он был ровно у тех, кому сырую фичу не
    дают, то есть у обычного фермера.
    """
    monkeypatch.setattr(B, "_FIELD_STATUS", {777})      # демо только у 777
    closed = B._why_markup(555, "T-1", rid=42)
    cbs = [b.callback_data for row in closed.inline_keyboard for b in row]
    assert cbs == ["fb:up:42", "fb:down:42"], "оценка ушла вместе с «почему»"
    # У демо-чата обе кнопки на месте — гейт всё ещё работает.
    assert len(B._why_markup(777, "T-1", rid=42).inline_keyboard) == 2


def test_advice_without_a_journal_id_keeps_the_plain_menu(monkeypatch):
    """Оценивать нечего — под советом обычное меню, как было.

    Пустую инлайн-клавиатуру Telegram не примет, а id рекомендации
    появляется только после записи в журнал: в утреннем пуше первое
    сообщение уходит без него.
    """
    monkeypatch.setattr(B, "_FIELD_STATUS", {777})
    assert B._why_markup(555, "T-1").keyboard                # reply-меню
