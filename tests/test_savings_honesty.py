"""
savings(): три дефекта аудита 18.08.2026, закрытые одним правилом
«молчание — не экономия» и дедупликацией по дню.

/tejaldi открыт жюри и ляжет в акт с Фаррухом — каждая цифра отсюда
обязана быть защитимой. Каждый тест ниже — способ завысить или занизить
экономию, который УЖЕ был найден в коде.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date, timedelta

import pytest

from suv.ledger import SILENT_GAP_INTERVALS, Ledger
from suv.messages import savings_text


@dataclass
class _Field:
    field_id: str = "T-1"
    ndvi: float | None = None
    ndvi_date: date | None = None


@dataclass
class _Rec:
    field: _Field = dc_field(default_factory=_Field)
    generated_on: date = dc_field(default_factory=date.today)
    action_day: date | None = None
    gross_mm: float = 27.8
    gross_m3: float = 576.0
    reason_key: str = "threshold_reached"
    plan: list = dc_field(default_factory=list)


SESSION = 576.0   # сеанс Фарруха: 24 ч × 24 м³/ч


def _ledger(tmp_path, baseline_per_ha=288.0, interval=4):
    led = Ledger(tmp_path / "t.db")
    led.upsert_field(
        field_id="T-1", name="Olmazor", owner_chat_id=1, hectares=2.0,
        lat=39.0, lon=67.0, elevation_m=700.0, crop_key="apple",
        soil_key="sandy_loam", planting_date="2018-03-20",
        irrigation_method="drip", water_table_depth_m=40.0,
        baseline_m3_per_ha=baseline_per_ha, baseline_interval_days=interval)
    return led


def _confirm(led, day, m3=SESSION):
    rid = led.log_recommendation(_Rec(generated_on=day), "test")
    led.log_action(rid, followed=True, actual_day=day, actual_m3=m3,
                   source="farmer")
    return rid


# ----------------------------------------------- 1. молчание — не экономия

def test_unmarked_irrigations_between_two_confirmations_earn_nothing(tmp_path):
    """КРИТИЧЕСКОЕ из аудита: рекомендации с 1 июня, фермер нажал ✅ только
    1 июня и 1 августа. Раньше: база 16 поливов = 9217 м³, расход 1152,
    «сэкономлено 8065 м³» — при том что все два месяца он поливал как
    обычно, просто не отмечал. Теперь 61 день молчания в счёт не входит."""
    led = _ledger(tmp_path)
    jun1 = date(2026, 6, 1)
    _confirm(led, jun1)
    for d in range(1, 61):          # пуш ходит каждое утро, отметок нет
        led.log_recommendation(_Rec(generated_on=jun1 + timedelta(days=d)), "test")
    _confirm(led, date(2026, 8, 1))
    s = led.savings("T-1")
    assert s.followed == 2
    assert s.metered_m3 == pytest.approx(2 * SESSION)
    assert s.baseline_m3 == pytest.approx(2 * SESSION)
    assert s.saved_m3 == pytest.approx(0.0)
    assert s.silent_days == 61
    assert "61" in savings_text(s, "ru") and "61" in savings_text(s, "uz")


def test_bot_stretching_the_interval_still_counts_as_saving(tmp_path):
    """Совет бота честно растянул интервал (дождь): 8 дней между отметками
    при привычке 4 — это два привычных полива против одного, экономия
    одного сеанса. Порог молчания не должен съедать настоящую экономию."""
    led = _ledger(tmp_path)
    start = date(2026, 7, 1)
    _confirm(led, start)
    _confirm(led, start + timedelta(days=8))
    s = led.savings("T-1")
    assert s.silent_days == 0
    assert s.baseline_m3 == pytest.approx(3 * SESSION)     # дни 0, 4, 8
    assert s.metered_m3 == pytest.approx(2 * SESSION)
    assert s.saved_m3 == pytest.approx(SESSION)


def test_silence_threshold_is_two_old_intervals(tmp_path):
    """Граница: 2 × interval ещё покрыто, 2 × interval + 1 — уже молчание."""
    assert SILENT_GAP_INTERVALS == 2
    led = _ledger(tmp_path)
    start = date(2026, 7, 1)
    _confirm(led, start)
    _confirm(led, start + timedelta(days=9))
    s = led.savings("T-1")
    assert s.silent_days == 9
    assert s.baseline_m3 == pytest.approx(2 * SESSION)     # только сами отметки
    assert s.saved_m3 == pytest.approx(0.0)


def test_first_confirmation_long_after_first_recommendation_is_silence(tmp_path):
    """Рекомендации шли с 1 июня, первое ✅ — 1 августа. Окно без единой
    отметки не даёт ни одного «привычного» полива — правило 2."""
    led = _ledger(tmp_path)
    jun1 = date(2026, 6, 1)
    led.log_recommendation(_Rec(generated_on=jun1), "test")
    _confirm(led, date(2026, 8, 1))
    s = led.savings("T-1")
    assert s.baseline_m3 == pytest.approx(SESSION)
    assert s.saved_m3 == pytest.approx(0.0)
    assert s.silent_days == 61


# ----------------------------------------------- 2. один день — один полив

def test_same_day_confirmed_on_two_recommendations_counts_once(tmp_path):
    """Утренний пуш (rec 1) + дневная кнопка (rec 2): фермер нажал ✅ на
    обе — один полив, не два. Раньше metered=1152 при базе 576 →
    /tejaldi показывал −576 фермеру, который выполнил совет ровно раз."""
    led = _ledger(tmp_path)
    day = date(2026, 7, 10)
    r1 = led.log_recommendation(_Rec(generated_on=day), "test")
    r2 = led.log_recommendation(_Rec(generated_on=day), "test")
    led.log_action(r1, followed=True, actual_day=day, actual_m3=SESSION, source="farmer")
    led.log_action(r2, followed=True, actual_day=day, actual_m3=600.0, source="farmer")
    s = led.savings("T-1")
    assert s.followed == 1
    assert s.metered_m3 == pytest.approx(600.0)    # из дублей — больший объём
    assert s.baseline_m3 == pytest.approx(SESSION)
    assert s.saved_m3 == pytest.approx(SESSION - 600.0)


def test_ledger_knows_about_actions_on_a_day(tmp_path):
    """Хендлер «✅ Suv berdim» спрашивает журнал по дню, а не только по id."""
    led = _ledger(tmp_path)
    day = date(2026, 7, 10)
    rid = led.log_recommendation(_Rec(generated_on=day), "test")
    assert not led.has_action_on_day("T-1", day)
    led.log_action(rid, followed=True, actual_day=day, actual_m3=SESSION, source="farmer")
    assert led.has_action_on_day("T-1", day)
    assert not led.has_action_on_day("T-1", day + timedelta(days=1))
    assert not led.has_action_on_day("T-2", day)


# ----------------------------------------------- 3. межсезонье

def test_winter_gap_adds_no_phantom_irrigations(tmp_path):
    """Последний полив сезона 15.10, первый полив следующего 15.04:
    раньше +45 «зимних поливов по привычке» ≈ 26 000 м³ экономии в
    первое же утро второго сезона. Теперь зима — молчание."""
    led = _ledger(tmp_path)
    _confirm(led, date(2026, 10, 11))
    _confirm(led, date(2026, 10, 15))
    before = led.savings("T-1")
    _confirm(led, date(2027, 4, 15))
    after = led.savings("T-1")
    assert before.saved_m3 == pytest.approx(0.0)            # 2 привычных / 2 факт
    assert after.baseline_m3 - before.baseline_m3 == pytest.approx(SESSION)
    assert after.saved_m3 == pytest.approx(0.0)
    assert after.silent_days == (date(2027, 4, 15) - date(2026, 10, 15)).days


# ----------------------------------------------- прежние правила целы

def test_pouring_exactly_the_old_norm_is_zero_not_minus(tmp_path):
    led = _ledger(tmp_path)
    start = date(2026, 7, 1)
    for d in range(0, 21, 4):
        _confirm(led, start + timedelta(days=d))
    s = led.savings("T-1")
    assert s.baseline_m3 == pytest.approx(6 * SESSION)
    assert s.metered_m3 == pytest.approx(6 * SESSION)
    assert s.saved_m3 == pytest.approx(0.0)
    assert s.silent_days == 0


def test_pouring_more_often_than_the_old_norm_shows_overuse(tmp_path):
    """Каждые 3 дня вместо 4: за 9 дней 4 полива против 3 привычных —
    честный минус на один сеанс, а не на три (floor по отрезкам терял бы
    дробные интервалы)."""
    led = _ledger(tmp_path)
    start = date(2026, 7, 1)
    for d in (0, 3, 6, 9):
        _confirm(led, start + timedelta(days=d))
    s = led.savings("T-1")
    assert s.baseline_m3 == pytest.approx(3 * SESSION)
    assert s.saved_m3 == pytest.approx(-SESSION)


def test_no_silent_line_when_journal_is_complete():
    from suv.ledger import SavingsSummary
    s = SavingsSummary("T-1", 3, 3, 1728.0, 1728.0, 0.0, False, True, 0)
    assert "без отметок" not in savings_text(s, "ru")
    assert "belgisiz" not in savings_text(s, "uz")
