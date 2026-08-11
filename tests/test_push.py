"""
Логика утреннего автопуша: когда бот пишет сам, а когда молчит.

Правило одно, но с двумя сторонами: срочное доходит всегда (поле не
должно сохнуть из-за забытой кнопки), спокойное — не превращается в спам.
"""

from __future__ import annotations

from datetime import date, timedelta

from bot.main import PUSH_QUIET_DAYS, push_due

TODAY = date(2026, 8, 11)


def test_urgent_always_pushes():
    """«Поливать сегодня/завтра» шлётся даже если вчера уже слали."""
    assert push_due(True, TODAY - timedelta(days=1), TODAY)
    assert push_due(True, TODAY, TODAY)


def test_never_contacted_pushes():
    """Новому владельцу первый совет уходит без всяких условий."""
    assert push_due(False, None, TODAY)


def test_quiet_period_holds():
    """Спокойный совет уходил вчера — сегодня молчим."""
    assert not push_due(False, TODAY - timedelta(days=1), TODAY)
    assert not push_due(False, TODAY - timedelta(days=PUSH_QUIET_DAYS - 1),
                        TODAY)


def test_quiet_period_expires():
    """Прошло PUSH_QUIET_DAYS — напоминаем о себе."""
    assert push_due(False, TODAY - timedelta(days=PUSH_QUIET_DAYS), TODAY)
    assert push_due(False, TODAY - timedelta(days=30), TODAY)
