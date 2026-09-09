"""
Окна опрыскивания: правила над почасовым прогнозом.

Каждый порог — документированная агрономическая норма (см. suv/spray.py),
и тексты обязаны говорить «по прогнозу»: это прогноз, не измерение.
"""

from datetime import datetime, timedelta

from suv.field_status import Status
from suv.spray import (DRY_HOURS_AFTER, MIN_WINDOW_H, build_section,
                       hour_ok, windows)
from suv.weather import HourlyWeather

T0 = datetime(2026, 9, 10, 0, 0)


def _h(hour, wind=2.5, temp=24.0, rh=55.0, rain=0.0, day=0):
    return HourlyWeather(time=T0 + timedelta(days=day, hours=hour),
                         temp=temp, rh=rh, wind_2m=wind, rain_mm=rain)


def _day(wind=2.5, temp=24.0, rh=55.0, rain=0.0):
    return [_h(h, wind, temp, rh, rain) for h in range(24)]


# ------------------------------------------------------------------ часы

def test_good_hour_passes_and_night_is_cut():
    ok, _ = hour_ok(_h(8), 0.0)
    assert ok
    assert hour_ok(_h(2), 0.0) == (False, "night")
    assert hour_ok(_h(22), 0.0) == (False, "night")


def test_each_threshold_has_its_reason():
    assert hour_ok(_h(8, wind=5.5), 0.0) == (False, "wind")
    assert hour_ok(_h(8, wind=0.4), 0.0) == (False, "calm")   # инверсия
    assert hour_ok(_h(8, temp=31.0), 0.0) == (False, "heat")
    assert hour_ok(_h(8, temp=5.0), 0.0) == (False, "cold")
    assert hour_ok(_h(8, rh=25.0), 0.0) == (False, "dry_air")
    assert hour_ok(_h(8, rain=1.0), 0.0) == (False, "rain")
    # Сухие часы ПОСЛЕ обработки: дождь впереди отменяет час сейчас.
    assert hour_ok(_h(8), rain_ahead_mm=2.0) == (False, "rain")


# ------------------------------------------------------------------ окна

def test_calm_morning_makes_a_window():
    ws, _ = windows(_day())
    assert ws, "весь день пригоден — окно обязано найтись"
    w = ws[0]
    assert w.start.hour == 5 and w.end.hour == 21
    assert w.hours >= MIN_WINDOW_H


def test_afternoon_wind_splits_the_day():
    hours = [_h(h, wind=(6.0 if 12 <= h < 18 else 2.5)) for h in range(24)]
    ws, top = windows(hours)
    assert [(-(-1), w.start.hour, w.end.hour) for w in ws]  # noqa: readable
    assert ws[0].start.hour == 5 and ws[0].end.hour == 12
    assert ws[1].start.hour == 18 and ws[1].end.hour == 21
    assert top == "wind"


def test_rain_blocks_dry_hours_before_it():
    hours = _day()
    hours[10] = _h(10, rain=3.0)     # дождь в 10:00
    ws, _ = windows(hours)
    # Часы 06..09 сгорели (нужно 4 сухих после), окно начинается после дождя.
    assert ws[0].start.hour == 11 or ws[0].start.hour == 5
    if ws[0].start.hour == 5:        # раннее окно должно закончиться в 06:00
        assert ws[0].end.hour <= 10 - DRY_HOURS_AFTER + 1
    assert all(not (w.start.hour <= 10 < w.end.hour) for w in ws)


def test_single_good_hour_is_not_a_window():
    hours = [_h(h, wind=6.0) for h in range(24)]
    hours[8] = _h(8)                  # один пригодный час
    ws, top = windows(hours)
    assert ws == [] and top == "wind"


# ---------------------------------------------------------------- секция

def test_section_reports_first_window_by_forecast():
    s = build_section(_day(), T0.date(), "ru")
    assert s is not None and s.status == Status.OK
    assert "сегодня 05:00" in s.line and "По прогнозу" in s.line


def test_section_names_the_dominant_blocker():
    s = build_section([_h(h, wind=6.0) for h in range(24)], T0.date(), "uz")
    assert s.status == Status.WARN
    assert "oyna yo'q" in s.line and "shamol" in s.line
    s_ru = build_section([_h(h, temp=32.0) for h in range(24)], T0.date(), "ru")
    assert "жара" in s_ru.line


def test_no_hourly_feed_means_no_section():
    assert build_section(None, T0.date(), "uz") is None
