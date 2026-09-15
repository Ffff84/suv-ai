"""
Болезни для культур, добавленных 15.09.2026: та же планка, что у яблони.

Статус красят только сверенные модели (Хаттон; 10-10-10; ядро индекса
Gubler–Thomas), фоновые культуры — информационная секция без статуса,
непокрытые культуры не получают секцию вовсе.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from suv.disease import (BG_ONLY_CROPS, DISEASE_CROPS, STATUS_CROPS,
                         build_report, build_section, gt_index, humid_days,
                         hutton_days, rule_10_10_10_days, wet_nights)
from suv.field_status import Status
from suv.weather import HourlyWeather

NOW = datetime(2026, 9, 14, 12, 0)
NOW_JUL = datetime(2026, 7, 10, 12, 0)


def _h(t: datetime, temp=18.0, rh=50.0, rain=0.0):
    return HourlyWeather(time=t, temp=temp, rh=rh, wind_2m=2.0, rain_mm=rain)


def _day(d: date, temp=18.0, rh=50.0, rain_h: int = 0, rh90_h: int = 0,
         hot_h: int = 0):
    """Полные сутки: rain_h часов с дождём, rh90_h часов при RH 92,
    hot_h подряд часов при 25° (для индекса GT)."""
    out = []
    for i in range(24):
        t = temp
        rh_i = rh
        rain = 0.0
        if i < rh90_h:
            rh_i = 92.0
        if i < rain_h:
            rain = 1.0
        if 10 <= i < 10 + hot_h:
            t = 25.0
        out.append(_h(datetime(d.year, d.month, d.day, i), temp=t,
                      rh=rh_i, rain=rain))
    return out


# ------------------------------------------------------------- реестр

def test_registry_is_partitioned_and_pear_absent():
    assert STATUS_CROPS & BG_ONLY_CROPS == set()
    assert DISEASE_CROPS == STATUS_CROPS | BG_ONLY_CROPS
    # у груши свой возбудитель парши — таблицу Миллса не переносим
    assert "pear" not in DISEASE_CROPS


def test_uncovered_crop_gets_no_section():
    hours = _day(date(2026, 9, 12)) + _day(date(2026, 9, 13))
    for crop in ("cotton", "maize", "pomegranate", "alfalfa"):
        assert build_section(hours, crop, NOW) is None


# ------------------------------------------------------------- Хаттон

def test_hutton_two_consecutive_qualifying_days_fire():
    hours = (_day(date(2026, 9, 12), temp=14, rh90_h=6)
             + _day(date(2026, 9, 13), temp=14, rh90_h=6))
    assert hutton_days(hours) == [date(2026, 9, 13)]


def test_hutton_five_rh_hours_are_not_enough():
    hours = (_day(date(2026, 9, 12), temp=14, rh90_h=5)
             + _day(date(2026, 9, 13), temp=14, rh90_h=6))
    assert hutton_days(hours) == []


def test_hutton_one_cold_hour_breaks_min_temp():
    hours = (_day(date(2026, 9, 12), temp=14, rh90_h=6)
             + _day(date(2026, 9, 13), temp=14, rh90_h=6))
    cold = hours[30]
    hours[30] = _h(cold.time, temp=9.0, rh=cold.rh, rain=cold.rain_mm)
    assert hutton_days(hours) == []


def test_potato_section_warns_and_names_the_model():
    hours = (_day(date(2026, 9, 12), temp=14, rh90_h=6)
             + _day(date(2026, 9, 13), temp=14, rh90_h=6))
    s = build_section(hours, "potato", NOW)
    assert s.status is Status.WARN
    assert "Fitoftoroz" in s.line and "Hutton" in s.line
    s_ru = build_section(hours, "tomato", NOW, lang="ru")
    assert "Фитофтороз" in s_ru.line


def test_potato_quiet_week_is_ok():
    hours = _day(date(2026, 9, 12)) + _day(date(2026, 9, 13))
    s = build_section(hours, "potato_summer", NOW)
    assert s.status is Status.OK


# ------------------------------------------------------------ 10-10-10

def test_rule_10_10_10_needs_rain_warmth_and_season():
    warm_rain = (_day(date(2026, 7, 7), temp=16, rain_h=6)
                 + _day(date(2026, 7, 8), temp=16, rain_h=6))
    assert rule_10_10_10_days(warm_rain, 2026) == [date(2026, 7, 8)]
    # мало дождя
    drizzle = (_day(date(2026, 7, 7), temp=16, rain_h=4)
               + _day(date(2026, 7, 8), temp=16, rain_h=4))
    assert rule_10_10_10_days(drizzle, 2026) == []
    # вне календарного окна побега
    autumn = (_day(date(2026, 9, 12), temp=16, rain_h=6)
              + _day(date(2026, 9, 13), temp=16, rain_h=6))
    assert rule_10_10_10_days(autumn, 2026) == []


def test_grape_section_event_warns():
    hours = (_day(date(2026, 7, 7), temp=16, rain_h=6)
             + _day(date(2026, 7, 8), temp=16, rain_h=6))
    s = build_section(hours, "grape", NOW_JUL)
    assert s.status is Status.WARN
    assert "10-10-10" in s.line


# ------------------------------------------------------------ индекс GT

def test_gt_index_climbs_with_hot_runs_and_floors_at_zero():
    d0 = date(2026, 9, 1)
    hot = []
    for i in range(4):
        hot += _day(d0 + timedelta(days=i), temp=18, hot_h=7)
    idx, n = gt_index(hot, NOW)
    assert (idx, n) == (80, 4)
    cool = []
    for i in range(4):
        cool += _day(d0 + timedelta(days=i), temp=18)
    assert gt_index(cool, NOW)[0] == 0


def test_gt_high_pressure_alone_warns_without_event():
    d0 = date(2026, 9, 8)
    hours = []
    for i in range(5):
        hours += _day(d0 + timedelta(days=i), temp=18, hot_h=7)
    s = build_section(hours, "grape", NOW)
    assert s.status is Status.WARN
    assert "100" in s.line or "80" in s.line


# ---------------------------------------------------------------- фон

def test_bg_crops_never_color_field_status():
    hours = _day(date(2026, 9, 12), rain_h=8) + _day(date(2026, 9, 13),
                                                     rain_h=8)
    for crop in sorted(BG_ONLY_CROPS):
        s = build_section(hours, crop, NOW)
        assert s is not None, crop
        assert s.status is Status.NO_DATA, crop
        assert s.informational, crop


def test_wet_nights_counts_humid_early_hours():
    d = date(2026, 9, 12)
    hours = []
    for i in range(24):
        rh = 96.0 if i < 4 else 50.0
        hours.append(_h(datetime(d.year, d.month, d.day, i), rh=rh))
    wet, total = wet_nights(hours, NOW)
    assert (wet, total) == (1, 1)


def test_humid_days_threshold_is_six_hours():
    d = date(2026, 5, 10)
    hours = _day(d, rh90_h=6) + _day(d + timedelta(days=1), rh90_h=5)
    wet, total = humid_days(hours, d, d + timedelta(days=1))
    assert (wet, total) == (1, 2)


# ------------------------------------------------------------- отчёты

def test_tomato_report_names_model_and_honesty_lines():
    hours = (_day(date(2026, 9, 12), temp=14, rh90_h=6)
             + _day(date(2026, 9, 13), temp=14, rh90_h=6))
    r = build_report(hours, "tomato", NOW, lang="ru")
    assert "Хаттон" in r and "P. infestans" in r
    assert "TOMCAST" in r and "статус не красим" in r
    assert "не диагноз" in r


def test_grape_report_names_sources_and_assumptions():
    hours = (_day(date(2026, 7, 7), temp=16, rain_h=6)
             + _day(date(2026, 7, 8), temp=16, rain_h=6))
    r = build_report(hours, "grape", NOW_JUL, lang="ru")
    assert "Baldacci" in r and "Gubler" in r
    assert "по календарю" in r or "календар" in r


def test_bg_reports_say_fon_not_status():
    hours = _day(date(2026, 9, 12)) + _day(date(2026, 9, 13))
    for crop, marker in (("winter_wheat", "Фузариоз"), ("onion", "DOWNCAST"),
                         ("melon", "прилёт спор"), ("cherry", "Монилиоз")):
        r = build_report(hours, crop, NOW, lang="ru")
        assert marker in r, crop
        assert "не диагноз" in r, crop


def test_new_crop_gate_keeps_raw_texts_from_farmer(monkeypatch):
    """Яблоня открыта по KASALLIK, новые культуры требуют ещё и
    KASALLIK_YANGI: иначе необкатанный текст про милдью пришёл бы прямо
    в виноградник Фарруха, который в KASALLIK уже вписан."""
    import bot.main as m
    AMIR, FARRUKH, STRANGER = 111, 222, 333
    monkeypatch.setattr(m, "_KASALLIK", {AMIR, FARRUKH})
    monkeypatch.setattr(m, "_KASALLIK_YANGI", {AMIR})
    assert m._kasallik_crop_open(AMIR, "apple")
    assert m._kasallik_crop_open(FARRUKH, "apple")
    assert m._kasallik_crop_open(AMIR, "grape")
    assert not m._kasallik_crop_open(FARRUKH, "grape")
    assert not m._kasallik_crop_open(STRANGER, "apple")
    # пустой YANGI закрывает новое даже гейт-чатам — семантика кабинета
    monkeypatch.setattr(m, "_KASALLIK_YANGI", set())
    assert not m._kasallik_crop_open(AMIR, "potato")


def test_uz_is_default_report_language():
    hours = (_day(date(2026, 9, 12), temp=14, rh90_h=6)
             + _day(date(2026, 9, 13), temp=14, rh90_h=6))
    r = build_report(hours, "potato", NOW)
    assert "Fitoftoroz" in r and "tashxis emas" in r
