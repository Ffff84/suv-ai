"""
Парсер погоды: дыры в данных Open-Meteo не должны ронять ряд.

Null от API — не гипотеза: словили TypeError на живом 92-дневном архиве
(wind_speed_10m_max = null на границе архив/прогноз).
"""

from __future__ import annotations

from suv.et0 import et0
from suv.weather import parse_daily, wind_10m_to_2m


def _daily(**overrides):
    base = {
        "time": ["2026-06-01", "2026-06-02", "2026-06-03"],
        "temperature_2m_max": [30.0, 31.0, 32.0],
        "temperature_2m_min": [18.0, 19.0, 20.0],
        "relative_humidity_2m_mean": [45.0, 44.0, 43.0],
        "wind_speed_10m_max": [3.0, 3.1, 3.2],
        "shortwave_radiation_sum": [28.0, 28.5, 29.0],
        "precipitation_sum": [0.0, 1.2, 0.0],
    }
    base.update(overrides)
    return base


def test_clean_series_parses_fully():
    days = parse_daily(_daily())
    assert len(days) == 3
    assert days[1].rainfall == 1.2


def test_null_wind_survives_and_falls_back_to_hargreaves():
    days = parse_daily(_daily(wind_speed_10m_max=[3.0, None, 3.2]))
    assert len(days) == 3
    assert days[1].wind_2m is None
    ref, method = et0(days[1], lat_deg=39.6, elevation_m=700)
    assert method == "hargreaves"
    assert ref > 0


def test_null_temperature_truncates_series():
    days = parse_daily(_daily(temperature_2m_max=[30.0, None, 32.0]))
    assert len(days) == 1  # обрезка, не пропуск: дни должны идти подряд


def test_null_precipitation_is_zero():
    days = parse_daily(_daily(precipitation_sum=[None, 0.5, None]))
    assert days[0].rainfall == 0.0


def test_wind_conversion_passes_none_through():
    assert wind_10m_to_2m(None) is None
    assert 0.7 < wind_10m_to_2m(1.0) < 0.8
