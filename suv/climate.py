"""
Climate normals for the pilot districts, and a season generator.

THIS IS A FALLBACK, NOT A DATA SOURCE. In production the daily feed comes
from weather.py (Open-Meteo, free, no key, 16-day forecast). These normals
exist so the engine can be demonstrated, tested and back-tested offline —
including in this repo's CI, which has no internet.

Normals are long-term monthly averages for the stated station. Replace
with Uzhydromet series before any claim is made about a specific season.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from .et0 import DailyWeather, solar_radiation_from_sunshine


@dataclass(frozen=True)
class Station:
    key: str
    name_ru: str
    lat: float
    lon: float
    elevation_m: float
    # per month, index 0 = January
    t_max: tuple[float, ...]
    t_min: tuple[float, ...]
    rh_mean: tuple[float, ...]
    sunshine_frac: tuple[float, ...]  # n/N
    rain_mm: tuple[float, ...]
    wind_2m: float
    typical_water_table_m: float


STATIONS: dict[str, Station] = {
    "fergana": Station(
        key="fergana", name_ru="Фергана",
        lat=40.39, lon=71.78, elevation_m=580,
        t_max=(5, 8, 15, 23, 28, 33, 35, 34, 29, 22, 14, 7),
        t_min=(-4, -2, 4, 10, 15, 19, 21, 19, 14, 8, 3, -2),
        rh_mean=(75, 72, 65, 58, 50, 42, 40, 40, 45, 55, 65, 75),
        sunshine_frac=(0.42, 0.46, 0.52, 0.60, 0.70, 0.80, 0.82, 0.80, 0.75, 0.62, 0.48, 0.40),
        rain_mm=(20, 25, 35, 30, 25, 10, 5, 3, 5, 15, 20, 20),
        wind_2m=1.8,
        typical_water_table_m=1.8,
    ),
    "samarkand": Station(
        key="samarkand", name_ru="Самарканд",
        lat=39.65, lon=66.96, elevation_m=705,
        t_max=(6, 8, 14, 21, 27, 33, 35, 34, 29, 21, 14, 8),
        t_min=(-2, -1, 4, 9, 13, 17, 19, 17, 12, 7, 3, 0),
        rh_mean=(76, 74, 68, 60, 50, 38, 35, 35, 40, 55, 66, 74),
        sunshine_frac=(0.40, 0.44, 0.50, 0.58, 0.70, 0.82, 0.85, 0.84, 0.78, 0.62, 0.46, 0.38),
        rain_mm=(45, 50, 70, 55, 30, 6, 3, 1, 3, 25, 40, 45),
        wind_2m=2.0,
        typical_water_table_m=3.5,
    ),
    "bukhara": Station(
        key="bukhara", name_ru="Бухара",
        lat=39.77, lon=64.42, elevation_m=225,
        t_max=(6, 9, 16, 24, 30, 35, 37, 35, 30, 22, 15, 8),
        t_min=(-3, -1, 5, 11, 16, 20, 22, 19, 13, 7, 3, -1),
        rh_mean=(78, 74, 66, 55, 44, 33, 32, 34, 38, 50, 64, 76),
        sunshine_frac=(0.42, 0.46, 0.52, 0.62, 0.74, 0.85, 0.87, 0.86, 0.80, 0.66, 0.48, 0.40),
        rain_mm=(20, 20, 28, 20, 10, 2, 1, 1, 1, 8, 15, 18),
        wind_2m=2.4,
        typical_water_table_m=2.2,
    ),
}


def _interp_monthly(values: tuple[float, ...], d: date) -> float:
    """Smooth a monthly table into a daily value (mid-month anchored)."""
    m = d.month - 1
    day_frac = (d.day - 15) / 30.0
    nxt = (m + 1) % 12 if day_frac > 0 else (m - 1) % 12
    w = abs(day_frac)
    return values[m] * (1 - w) + values[nxt] * w


def synth_day(station: Station, d: date, wet_spell: bool = False) -> DailyWeather:
    """One synthetic day from normals, with a deterministic wobble."""
    doy = d.timetuple().tm_yday
    # deterministic pseudo-variation so runs are reproducible
    wob = math.sin(doy * 2.399) * 2.5

    t_max = _interp_monthly(station.t_max, d) + wob
    t_min = _interp_monthly(station.t_min, d) + wob * 0.6
    rh = min(95.0, max(15.0, _interp_monthly(station.rh_mean, d) - wob))
    frac = min(0.95, max(0.15, _interp_monthly(station.sunshine_frac, d) - abs(wob) * 0.02))

    n_hours = frac * 12.0
    rs = solar_radiation_from_sunshine(n_hours, doy, station.lat)
    rs = rs * (frac / max(frac, 0.01))  # keep the Angstrom result as-is

    monthly_rain = _interp_monthly(station.rain_mm, d)
    # concentrate the month's rain into a handful of events
    rain = 0.0
    if wet_spell or (doy % 9 == 0):
        rain = monthly_rain / 3.5
    if wob > 2.0 and monthly_rain > 15:
        rain += monthly_rain / 6.0

    return DailyWeather(
        doy=doy, t_max=round(t_max, 1), t_min=round(t_min, 1),
        rh_mean=round(rh, 1), wind_2m=station.wind_2m,
        solar_rad=round(rs, 2), rainfall=round(rain, 1),
    )


def season(station: Station, start: date, days: int) -> list[DailyWeather]:
    return [synth_day(station, start + timedelta(days=i)) for i in range(days)]
