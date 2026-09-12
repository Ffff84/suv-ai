"""
Логика утреннего автопуша: когда бот пишет сам, а когда молчит.

С 17.08.2026 сообщение приходит каждое утро (решение Амира: тишина
читается фермером как «бот умер»). Молчание осталось только одно —
когда совет уже уходил СЕГОДНЯ: пуш не должен дублировать сам себя
после рестарта или нажатую час назад кнопку.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import bot.main as B
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


def test_catchup_does_not_turn_a_midday_deploy_into_a_morning_round():
    """Догонять утро можно утром.

    push_catchup ставится на 15 секунд после КАЖДОГО старта, а старт —
    это каждый деплой. 12.09.2026 деплой в час дня разослал бы всем
    владельцам утренний обход по сообщению на поле: не потому что пора
    поливать, а потому что выложили код. Нижняя граница у окна была,
    верхней не было.
    """
    assert B.PUSH_HOUR_TASHKENT < B.PUSH_CATCHUP_UNTIL_HOUR <= 12

    calls = []

    async def _fake_daily_push(ctx, only_if_silent_today=False):
        calls.append(only_if_silent_today)

    class _Clock:
        hour = 6

    def _run(hour):
        _Clock.hour = hour
        calls.clear()
        import suv.clock as clock
        real_now, real_push = clock.now, B.daily_push
        clock.now = lambda: _Clock
        B.daily_push = _fake_daily_push
        try:
            asyncio.run(B.push_catchup(None))
        finally:
            clock.now, B.daily_push = real_now, real_push
        return list(calls)

    assert _run(5) == [], "до утреннего часа догонять нечего"
    assert _run(7) == [True], "пропущенное утро догоняется"
    assert _run(13) == [], "деплой в час дня — не утренний обход"
