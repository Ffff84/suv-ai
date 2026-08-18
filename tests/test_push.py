"""
Логика утреннего автопуша: когда бот пишет сам, а когда молчит.

С 17.08.2026 сообщение приходит каждое утро (решение Амира: тишина
читается фермером как «бот умер»). Молчание осталось только одно —
когда совет уже уходил СЕГОДНЯ: пуш не должен дублировать сам себя
после рестарта или нажатую час назад кнопку.
"""

from __future__ import annotations

from datetime import date, timedelta

from bot.main import PUSH_QUIET_DAYS, push_due

TODAY = date(2026, 8, 11)


def test_urgent_always_pushes():
    """«Поливать сегодня/завтра» шлётся даже если сегодня уже слали."""
    assert push_due(True, TODAY - timedelta(days=1), TODAY)
    assert push_due(True, TODAY, TODAY)


def test_never_contacted_pushes():
    """Новому владельцу первый совет уходит без всяких условий."""
    assert push_due(False, None, TODAY)


def test_pushes_every_morning():
    """Вчерашний совет не глушит сегодняшний: сообщение — каждый день."""
    assert PUSH_QUIET_DAYS == 1, \
        "каданс изменён — проверьте текст регистрации и ДЕПЛОЙ.md"
    assert push_due(False, TODAY - timedelta(days=1), TODAY)
    assert push_due(False, TODAY - timedelta(days=30), TODAY)


def test_no_duplicate_within_a_day():
    """Совет уже уходил сегодня (кнопкой или пушем) — второй раз молчим."""
    assert not push_due(False, TODAY, TODAY)
