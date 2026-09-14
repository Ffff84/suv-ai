"""
Болезни сада по метеоданным: риск, не диагноз.

Пороги — сверенные публикации (ревизия Миллса, BHWT из MARYBLYT, см.
suv/disease.py), и каждый экран обязан говорить «по метеомодели» и
заканчиваться осмотром листьев: увидеть болезнь может только глаз.

Гейт: KASALLIK_CHAT_IDS читается как у кабинета и укоса — пусто =
закрыто ВСЕМ. Сырая формулировка «условия заражения выполнились» без
обкатки не должна дойти до пилотного фермера.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta

import pytest

from suv.disease import (DRY_GAP_H, HEAVY_FACTOR, REPORT_MAX_DONE,
                         build_report, build_section, fire_blight_days,
                         mildew_background, mills_hours, scab_checks,
                         wet_windows)
from suv.field_status import Status
from suv.weather import HourlyWeather

NOW = datetime(2026, 9, 14, 12, 0)


def _h(t: datetime, temp=18.0, rh=50.0, rain=0.0):
    return HourlyWeather(time=t, temp=temp, rh=rh, wind_2m=2.0, rain_mm=rain)


def _wet_run(start: datetime, hours: int, temp=18.0, rain=1.0):
    return [_h(start + timedelta(hours=i), temp=temp, rain=rain)
            for i in range(hours)]


def _dry_run(start: datetime, hours: int, temp=18.0, rh=50.0):
    return [_h(start + timedelta(hours=i), temp=temp, rh=rh)
            for i in range(hours)]


# ------------------------------------------------------------- Миллс

def test_mills_matches_verified_table_points():
    """Опорные точки — из сверенной таблицы (Purdue/Penn State):
    6 ч на плато 17–23°, ~28 ч при 4°, края таблицы."""
    assert mills_hours(17.0) == 6.0
    assert mills_hours(20.0) == 6.0      # плато, не интерполяция к краю
    assert mills_hours(23.0) == 6.0
    assert mills_hours(4.0) == 27.8
    assert mills_hours(1.0) == 40.5
    assert mills_hours(26.0) == 11.3


def test_mills_interpolates_between_rows():
    v = mills_hours(4.5)
    assert 21.2 < v < 27.8


def test_mills_refuses_outside_the_table():
    """За краем таблицы порога нет — гадать нельзя."""
    assert mills_hours(0.5) is None
    assert mills_hours(27.0) is None


# ------------------------------------------------------- влажные окна

def test_rain_and_dew_hours_are_both_wet():
    hours = [_h(NOW, rain=1.0), _h(NOW + timedelta(hours=1), rh=95.0)]
    (w,) = wet_windows(hours)
    assert w.wet_hours == 2


def test_short_dry_gap_bridges_but_does_not_count():
    """Сухой разрыв короче DRY_GAP_H не прерывает окно (правило
    прерывистого увлажнения), но в часы влажности не идёт."""
    t0 = NOW
    hours = (_wet_run(t0, 4)
             + _dry_run(t0 + timedelta(hours=4), DRY_GAP_H - 2)
             + _wet_run(t0 + timedelta(hours=4 + DRY_GAP_H - 2), 5))
    (w,) = wet_windows(hours)
    assert w.wet_hours == 9              # только мокрые часы


def test_long_dry_gap_splits_windows():
    t0 = NOW
    hours = (_wet_run(t0, 4)
             + _dry_run(t0 + timedelta(hours=4), DRY_GAP_H)
             + _wet_run(t0 + timedelta(hours=4 + DRY_GAP_H), 5))
    assert len(wet_windows(hours)) == 2


# ------------------------------------------------------------- парша

def test_six_wet_hours_at_optimum_infect():
    (c,) = scab_checks(_wet_run(NOW, 6, temp=18.0))
    assert c.infected and not c.heavy
    assert c.incubation == (9, 10)


def test_double_threshold_is_heavy():
    (c,) = scab_checks(_wet_run(NOW, int(6 * HEAVY_FACTOR), temp=18.0))
    assert c.heavy


def test_below_threshold_is_safe():
    (c,) = scab_checks(_wet_run(NOW, 4, temp=18.0))
    assert not c.infected and c.incubation is None


def test_out_of_table_temperature_is_not_scored():
    (c,) = scab_checks(_wet_run(NOW, 30, temp=30.0))
    assert c.required_h is None and not c.infected


# ------------------------------------------------------------ бакожог

def _april_days(first: date, n: int, rain_on: set[date] = frozenset()):
    """Тёплые апрельские сутки: 12 ч по 28° (капают градусо-часы,
    ~116 в день) и 12 ч по 15° — среднесуточная ~21,5° ≥ 15,6°."""
    out = []
    for i in range(n):
        d = first + timedelta(days=i)
        for hh in range(24):
            rain = 0.5 if (d in rain_on and hh == 13) else 0.0
            out.append(_h(datetime(d.year, d.month, d.day, hh),
                          temp=28.0 if 8 <= hh < 20 else 15.0, rain=rain))
    return out


def test_bhwt_needs_all_four_letters():
    bloom = (date(2026, 4, 1), date(2026, 4, 30))
    wet_day = date(2026, 4, 3)
    hours = _april_days(date(2026, 3, 30), 6, rain_on={wet_day})
    risky, partial = fire_blight_days(hours, *bloom)
    assert risky == [wet_day]            # сухие тёплые дни риском не стали
    assert not partial


def test_bhwt_without_heat_accumulation_is_silent():
    """Прохладное цветение с дождём: 110 градусо-часов выше 18,3° не
    набраны — буква H держит модель молчаливой, как в MARYBLYT."""
    bloom = (date(2026, 4, 1), date(2026, 4, 30))
    hours = []
    for i in range(4):
        d = date(2026, 4, 1) + timedelta(days=i)
        for hh in range(24):
            # 20° днём даёт лишь ~1,7 ДЧ/час — за 4 дня далеко до 110.
            hours.append(_h(datetime(d.year, d.month, d.day, hh),
                            temp=20.0 if 8 <= hh < 20 else 16.0,
                            rain=0.5 if hh == 13 else 0.0))
    risky, _ = fire_blight_days(hours, *bloom)
    assert risky == []


def test_bhwt_partial_series_is_flagged():
    """Ряд не покрывает начало цветения — модель обязана сознаться,
    что сумма градусо-часов неполна."""
    bloom = (date(2026, 4, 1), date(2026, 4, 30))
    hours = _april_days(date(2026, 4, 5), 2)
    _, partial = fire_blight_days(hours, *bloom)
    assert partial


# --------------------------------------------------------------- роса

def test_mildew_counts_warm_humid_dry_days():
    d0 = NOW - timedelta(days=3)
    good = [_h(datetime(d0.year, d0.month, d0.day, hh), temp=20.0, rh=80.0)
            for hh in range(24)]
    rainy_day = d0 + timedelta(days=1)
    rainy = [_h(datetime(rainy_day.year, rainy_day.month, rainy_day.day, hh),
                temp=20.0, rh=80.0, rain=0.5) for hh in range(24)]
    fav, total = mildew_background(good + rainy, NOW)
    assert (fav, total) == (1, 2)        # дождь смыл второй день


# ------------------------------------------------------------- секция

def test_section_only_for_covered_crops_and_with_data():
    assert build_section(_wet_run(NOW, 6), "cotton", NOW) is None
    assert build_section(None, "apple", NOW) is None
    assert build_section([], "apple", NOW) is None


def test_quiet_week_is_ok_and_says_model():
    hours = _dry_run(NOW - timedelta(days=3), 24 * 5)
    s = build_section(hours, "apple", NOW, lang="ru")
    assert s.status is Status.OK
    assert "метеомодел" in s.hint.lower()
    assert s.action.callback == "fs:kasal"


def test_past_infection_warns_and_names_incubation():
    hours = _wet_run(NOW - timedelta(days=2), 8, temp=18.0)
    s = build_section(hours, "apple", NOW, lang="ru")
    assert s.status is Status.WARN
    assert "заражения выполнились" in s.line
    assert "проверьте листья" in s.hint


def test_heavy_infection_alerts():
    hours = _wet_run(NOW - timedelta(days=2), 14, temp=18.0)
    s = build_section(hours, "apple", NOW, lang="ru")
    assert s.status is Status.ALERT
    assert "сильное" in s.line


def test_forecast_window_warns_and_says_forecast():
    hours = _wet_run(NOW + timedelta(hours=6), 8, temp=16.0)
    s = build_section(hours, "apple", NOW, lang="ru")
    assert s.status is Status.WARN
    assert "по прогнозу" in s.line


def test_old_infection_does_not_color_today():
    """Окно старше недели статуса не красит: карточка про сейчас."""
    hours = (_wet_run(NOW - timedelta(days=10), 14, temp=18.0)
             + _dry_run(NOW - timedelta(days=5), 24 * 5))
    s = build_section(hours, "apple", NOW, lang="ru")
    assert s.status is Status.OK


# -------------------------------------------------------------- отчёт

def test_report_is_full_and_honest_ru():
    hours = (_dry_run(NOW - timedelta(days=6), 24)
             + _wet_run(NOW - timedelta(days=4), 14, temp=18.0)
             + _wet_run(NOW - timedelta(days=1), 3, temp=18.0)
             + _wet_run(NOW + timedelta(hours=12), 8, temp=16.0))
    text = build_report(hours, "apple", NOW, lang="ru", crop_name="Яблоня")
    assert "Миллса" in text
    assert "СИЛЬНОГО" in text            # 14 ч при пороге 6 ч
    assert "безопасно" in text           # 3 ч — не окно заражения
    assert "По прогнозу" in text
    assert "проверьте листья" in text
    assert "не диагноз" in text
    # Сентябрь: бакожог — про прошедшее цветение, а не пустая секция.
    assert "цветения" in text
    assert "не полевой замер" in text
    assert len(text) < 4096


def test_report_speaks_uzbek_by_default():
    hours = _wet_run(NOW - timedelta(days=2), 8, temp=18.0)
    text = build_report(hours, "apple", NOW, crop_name="Olma")
    assert "tashxis emas" in text
    assert "barglarni tekshiring" in text.lower()


def test_report_in_bloom_lists_bhwt_days_and_names_assumption():
    now = datetime(2026, 4, 6, 12, 0)
    hours = _april_days(date(2026, 3, 30), 8, rain_on={date(2026, 4, 3)})
    text = build_report(hours, "apple", now, lang="ru", crop_name="Яблоня")
    assert "03.04" in text
    assert "по календарю" in text        # цветение — допущение, вслух


def test_report_caps_listed_windows():
    hours = []
    for i in range(REPORT_MAX_DONE + 4):
        hours += _wet_run(NOW - timedelta(days=13) + timedelta(hours=36 * i),
                          8, temp=18.0)
    text = build_report(hours, "apple", NOW, lang="ru")
    assert text.count("→") <= REPORT_MAX_DONE
    assert len(text) < 4096


# --------------------------------------------------------------- гейт

@pytest.fixture()
def _restore_bot_module():
    """Гейт-тесты перечитывают бота с другим окружением; вернуть модуль
    в исходное состояние после очистки env (порядок как в
    test_demo_gate: фикстура создаётся раньше monkeypatch, поэтому её
    teardown идёт после его отката)."""
    yield
    import bot.main
    importlib.reload(bot.main)


def _reload_bot(monkeypatch, tmp_path, ids: str):
    monkeypatch.setenv("KASALLIK_CHAT_IDS", ids)
    monkeypatch.setenv("SUV_DB", str(tmp_path / "gate.db"))
    monkeypatch.setenv("TELEGRAM_TOKEN", "test:token")
    import bot.main as m
    return importlib.reload(m)


def test_gate_empty_means_closed_to_everyone(_restore_bot_module,
                                             monkeypatch, tmp_path):
    """Семантика кабинета и укоса, не allowlist'а: пустой .env не
    должен выкатить сырую секцию Фарруху."""
    m = _reload_bot(monkeypatch, tmp_path, "")
    assert not m._kasallik_open(555)


def test_gate_opens_only_listed_chats(_restore_bot_module,
                                      monkeypatch, tmp_path):
    m = _reload_bot(monkeypatch, tmp_path, "777")
    assert m._kasallik_open(777)
    assert not m._kasallik_open(555)
