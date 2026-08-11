"""
Формат сообщения фермеру.

Правило даты в скобках закреплено тестом: в 14-дневном окне прогноза
каждый день недели встречается дважды, и «в четверг» без даты уже один
раз прочитали как противоречие бота самому себе.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date, timedelta

from suv.messages import recommendation_text


@dataclass
class _Field:
    name: str = "Olmazor"
    hectares: float = 2.0


@dataclass
class _Rec:
    field: _Field = dc_field(default_factory=_Field)
    action_day: date | None = None
    gross_mm: float = 27.8
    gross_m3: float = 555.6
    reason_key: str = "threshold_approaching"
    days_until: int = 0
    plan: list = dc_field(default_factory=list)


def test_far_day_carries_the_date():
    day = date.today() + timedelta(days=5)
    rec = _Rec(action_day=day, days_until=5)
    stamp = f"({day.day:02d}.{day.month:02d})"
    assert stamp in recommendation_text(rec, "uz")
    assert stamp in recommendation_text(rec, "ru")


def test_far_date_carries_refinement_note():
    """За неделю и дальше — честное «дата уточнится»; вблизи — нет."""
    far = _Rec(action_day=date.today() + timedelta(days=9), days_until=9)
    assert "aniqlashadi" in recommendation_text(far, "uz")
    assert "уточнится" in recommendation_text(far, "ru")

    near = _Rec(action_day=date.today() + timedelta(days=3), days_until=3)
    assert "aniqlashadi" not in recommendation_text(near, "uz")
    assert "уточнится" not in recommendation_text(near, "ru")


def test_today_and_tomorrow_stay_plain():
    rec = _Rec(action_day=date.today(), days_until=0)
    assert "Bugun" in recommendation_text(rec, "uz")
    assert "(" not in recommendation_text(rec, "uz").split("\n")[2]

    rec = _Rec(action_day=date.today() + timedelta(days=1), days_until=1)
    assert "Ertaga" in recommendation_text(rec, "uz")
    assert "Завтра" in recommendation_text(rec, "ru")
