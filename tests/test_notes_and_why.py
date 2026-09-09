"""
Полевые заметки (фото + подпись + точка) и «почему такой совет».

Заметки — свидетельства для журнала и акта: фото «вода дошла до края»
убедительнее слов. «Почему» — детерминированное объяснение из уже
посчитанной рекомендации, сознательная замена AI-чату: каждая строка —
число из расчёта, его нельзя приукрасить.
"""

from datetime import date

import pytest

from suv.ledger import Ledger
from suv.messages import why_text


def _ledger(tmp_path):
    led = Ledger(tmp_path / "t.db")
    led.upsert_field(
        field_id="T-1", name="Olmazor", owner_chat_id=1, hectares=2.52,
        lat=39.5, lon=67.0, elevation_m=700.0, crop_key="apple",
        soil_key="sandy_loam", planting_date="2018-03-20",
        irrigation_method="drip", water_table_depth_m=40.0)
    return led


# ---------------------------------------------------------------- заметки

def test_note_round_trip(tmp_path):
    led = _ledger(tmp_path)
    nid = led.add_note("T-1", chat_id=1, taken_on=date(2026, 9, 9),
                       file_id="AgACAg...xyz", caption="suv chetga yetdi")
    rows = led.notes("T-1")
    assert len(rows) == 1
    n = rows[0]
    assert n["id"] == nid and n["taken_on"] == "2026-09-09"
    assert n["file_id"].startswith("AgACAg")
    assert n["caption"] == "suv chetga yetdi"
    assert n["lat"] is None and n["lon"] is None


def test_location_attaches_once_and_never_rewrites(tmp_path):
    led = _ledger(tmp_path)
    nid = led.add_note("T-1", 1, date(2026, 9, 9), "f1", None)
    assert led.attach_note_location(nid, 39.5581, 66.9961) is True
    # Вторая локация (прислана позже по другому поводу) место НЕ трогает.
    assert led.attach_note_location(nid, 41.0, 69.0) is False
    n = led.notes("T-1")[0]
    assert n["lat"] == pytest.approx(39.5581)
    assert n["lon"] == pytest.approx(66.9961)


def test_notes_are_newest_first_and_limited(tmp_path):
    led = _ledger(tmp_path)
    for i in range(5):
        led.add_note("T-1", 1, date(2026, 9, 1 + i), f"f{i}", None)
    rows = led.notes("T-1", limit=3)
    assert [r["taken_on"] for r in rows] == ["2026-09-05", "2026-09-04",
                                             "2026-09-03"]


def test_note_without_photo_is_allowed(tmp_path):
    """Текстовая заметка (на будущее) — file_id может быть пустым."""
    led = _ledger(tmp_path)
    led.add_note("T-1", 1, date(2026, 9, 9), None, "faqat matn")
    assert led.notes("T-1")[0]["file_id"] is None


# ------------------------------------------------------------- «почему»

def _rec(reason="threshold_approaching", act=date(2026, 9, 12), du=3):
    from dataclasses import dataclass, field as dc_field

    @dataclass
    class _P:
        depletion_mm: float = 34.0
        raw_mm: float = 41.0
        taw_mm: float = 78.0
        etc_mm: float = 4.4
        et0_mm: float = 5.6
        kc: float = 0.79
        kc_source: str = "calendar+satellite (60% satellite)"
        rain_mm: float = 0.0

    @dataclass
    class _F:
        name: str = "Olmazor"

    @dataclass
    class _R:
        field: _F = dc_field(default_factory=_F)
        generated_on: date = date(2026, 9, 9)
        action_day: date | None = act
        days_until: int = du
        reason_key: str = reason
        plan: list = dc_field(default_factory=lambda: [_P() for _ in range(14)])

    return _R()


def test_why_text_is_numbers_from_the_plan():
    t = why_text(_rec(), last_irr=date(2026, 9, 5), lang="ru")
    assert "34 мм" in t.replace("израсходовано ", "израсходовано ") or "34" in t
    assert "порога 41" in t
    assert "ET0 5.6" in t and "Kc 0.79" in t
    assert "60% satellite" in t
    assert "Последний полив: 05.09 (4 дн. назад)" in t
    assert "полив назначен" in t and "12.09" in t


def test_why_text_uzbek_and_no_anchor():
    t = why_text(_rec(), last_irr=None, lang="uz")
    assert "Tuproq zaxirasi" in t and "chegara" in t
    assert "noma'lum" in t          # якоря нет — честно сказано


def test_why_text_harvest_hold_and_degraded():
    r = _rec(reason="harvest_hold", act=None, du=-1)
    t = why_text(r, last_irr=date(2026, 9, 1), lang="ru", degraded=True)
    assert "съём урожая" in t and "остановлен сознательно" in t
    assert "по нормам" in t         # деградация названа прямо


def test_why_text_no_action_uses_reason_dictionary():
    r = _rec(reason="soil_still_wet", act=None, du=-1)
    t = why_text(r, last_irr=date(2026, 9, 7), lang="ru")
    assert "Почва ещё достаточно влажная" in t
