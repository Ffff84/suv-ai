"""
Weather feed — Open-Meteo.

Chosen deliberately: free, no API key, no registration, 16-day forecast,
and it serves Uzbekistan at ~11 km resolution. For a 12-day pilot, "no
key required" is worth more than marginal accuracy — you cannot wait a
week for a paid provider's onboarding.

Upgrade path once funded: Uzhydromet station series for calibration, and
ERA5-Land reanalysis for back-testing past seasons.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import requests

from .et0 import DailyWeather

BASE = "https://api.open-meteo.com/v1/forecast"

DAILY_VARS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "relative_humidity_2m_mean",
    "wind_speed_10m_max",
    "shortwave_radiation_sum",
    "precipitation_sum",
]


def wind_10m_to_2m(u10: float | None) -> float | None:
    """
    FAO-56 eq. 47. Open-Meteo reports wind at 10 m; ET0 needs it at 2 m.
        u2 = u10 * 4.87 / ln(67.8 * 10 - 5.42)  ->  u10 * 0.748
    Skipping this conversion inflates ET0 by roughly 5-10% on windy days.

    None проходит насквозь: et0() сам уйдёт в Hargreaves. Раньше здесь
    был TypeError, и один пустой день в 90-дневном архиве ронял весь ряд.
    """
    if u10 is None:
        return None
    return u10 * 4.87 / math.log(67.8 * 10.0 - 5.42)


def parse_daily(d: dict) -> list[DailyWeather]:
    """
    Превратить блок daily из ответа Open-Meteo в список дней.

    Правила устойчивости к дырам в данных (у Open-Meteo они бывают,
    особенно на границе архив/прогноз):
      - нет температуры — день бесполезен, ряд обрезается на нём;
      - нет влажности/ветра/радиации — день остаётся, et0() посчитает
        по Hargreaves (только температура);
      - нет осадков — считаем 0.
    Обрезка, а не пропуск: календарная непрерывность важнее длины,
    симуляция ходит по дням строго подряд.
    """
    out: list[DailyWeather] = []
    for i, iso in enumerate(d["time"]):
        t_max = d["temperature_2m_max"][i]
        t_min = d["temperature_2m_min"][i]
        if t_max is None or t_min is None:
            break
        day = datetime.strptime(iso, "%Y-%m-%d").date()
        out.append(DailyWeather(
            doy=day.timetuple().tm_yday,
            t_max=t_max,
            t_min=t_min,
            rh_mean=d["relative_humidity_2m_mean"][i],
            wind_2m=wind_10m_to_2m(d["wind_speed_10m_max"][i]),
            # Open-Meteo returns MJ/m2 already for shortwave_radiation_sum
            solar_rad=d["shortwave_radiation_sum"][i],
            rainfall=d["precipitation_sum"][i] or 0.0,
        ))
    return out


def fetch_forecast(lat: float, lon: float, days: int = 16,
                   past_days: int = 0, timeout: int = 20) -> list[DailyWeather]:
    """
    Pull the daily series for one field: past_days of history followed by
    `days` of forecast, in one call.

    Raises on network or schema failure rather than silently returning
    partial data — a recommendation built on half a forecast is worse
    than no recommendation, because the farmer cannot tell the difference.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ",".join(DAILY_VARS),
        "forecast_days": min(days, 16),
        # Past days let us warm-start the water balance from the farmer's
        # last irrigation instead of pretending the field is full today.
        "past_days": min(past_days, 92),
        "timezone": "Asia/Tashkent",
        "wind_speed_unit": "ms",
    }
    r = requests.get(BASE, params=params, timeout=timeout)
    r.raise_for_status()
    out = parse_daily(r.json()["daily"])
    if not out:
        raise ValueError("Open-Meteo вернул ряд без пригодных дней")
    return out
