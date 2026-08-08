"""
Reference evapotranspiration (ET0) — FAO Irrigation & Drainage Paper 56.

Two methods:
  penman_monteith  — the standard. Needs Tmax, Tmin, RH, wind, radiation.
  hargreaves       — fallback when radiation/humidity are missing.

All equations reference FAO-56 chapter 4. Equation numbers are in the
docstrings so an agronomist can audit this file against the paper.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SOLAR_CONSTANT = 0.0820  # MJ m-2 min-1
STEFAN_BOLTZMANN = 4.903e-9  # MJ K-4 m-2 day-1
ALBEDO_GRASS = 0.23


@dataclass
class DailyWeather:
    """One day of observations for one field."""

    doy: int  # day of year, 1..366
    t_max: float  # °C
    t_min: float  # °C
    rh_mean: float | None = None  # %
    wind_2m: float | None = None  # m s-1 at 2 m
    solar_rad: float | None = None  # MJ m-2 day-1, measured
    rainfall: float = 0.0  # mm

    @property
    def t_mean(self) -> float:
        return (self.t_max + self.t_min) / 2.0


def saturation_vapour_pressure(t: float) -> float:
    """e°(T), kPa. FAO-56 eq. 11."""
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


def slope_vapour_pressure_curve(t_mean: float) -> float:
    """Delta, kPa °C-1. FAO-56 eq. 13."""
    es = saturation_vapour_pressure(t_mean)
    return 4098.0 * es / ((t_mean + 237.3) ** 2)


def atmospheric_pressure(elevation_m: float) -> float:
    """P, kPa. FAO-56 eq. 7."""
    return 101.3 * ((293.0 - 0.0065 * elevation_m) / 293.0) ** 5.26


def psychrometric_constant(elevation_m: float) -> float:
    """gamma, kPa °C-1. FAO-56 eq. 8."""
    return 0.000665 * atmospheric_pressure(elevation_m)


def extraterrestrial_radiation(doy: int, lat_deg: float) -> float:
    """Ra, MJ m-2 day-1. FAO-56 eq. 21."""
    lat = math.radians(lat_deg)
    dr = 1.0 + 0.033 * math.cos(2.0 * math.pi * doy / 365.0)  # eq. 23
    decl = 0.409 * math.sin(2.0 * math.pi * doy / 365.0 - 1.39)  # eq. 24

    x = -math.tan(lat) * math.tan(decl)
    x = max(-1.0, min(1.0, x))  # guard polar day/night
    ws = math.acos(x)  # eq. 25

    return (
        24.0 * 60.0 / math.pi
        * SOLAR_CONSTANT
        * dr
        * (ws * math.sin(lat) * math.sin(decl)
           + math.cos(lat) * math.cos(decl) * math.sin(ws))
    )


def daylight_hours(doy: int, lat_deg: float) -> float:
    """N, hours. FAO-56 eq. 34."""
    lat = math.radians(lat_deg)
    decl = 0.409 * math.sin(2.0 * math.pi * doy / 365.0 - 1.39)
    x = -math.tan(lat) * math.tan(decl)
    x = max(-1.0, min(1.0, x))
    return 24.0 / math.pi * math.acos(x)


def solar_radiation_from_sunshine(n_hours: float, doy: int, lat_deg: float,
                                  a_s: float = 0.25, b_s: float = 0.50) -> float:
    """Rs from sunshine duration. FAO-56 eq. 35."""
    ra = extraterrestrial_radiation(doy, lat_deg)
    n = daylight_hours(doy, lat_deg)
    return (a_s + b_s * n_hours / n) * ra


def net_radiation(rs: float, doy: int, lat_deg: float, elevation_m: float,
                  t_max: float, t_min: float, ea: float) -> float:
    """Rn, MJ m-2 day-1. FAO-56 eq. 38-40."""
    ra = extraterrestrial_radiation(doy, lat_deg)
    rso = (0.75 + 2e-5 * elevation_m) * ra  # eq. 37, clear-sky

    rns = (1.0 - ALBEDO_GRASS) * rs  # eq. 38

    tmax_k4 = (t_max + 273.16) ** 4
    tmin_k4 = (t_min + 273.16) ** 4
    cloud = 1.35 * min(rs / rso, 1.0) - 0.35 if rso > 0 else 0.05
    rnl = (
        STEFAN_BOLTZMANN
        * ((tmax_k4 + tmin_k4) / 2.0)
        * (0.34 - 0.14 * math.sqrt(max(ea, 0.0)))
        * cloud
    )  # eq. 39
    return rns - rnl


def penman_monteith(w: DailyWeather, lat_deg: float, elevation_m: float) -> float:
    """
    ET0 in mm/day. FAO-56 eq. 6.

    Requires rh_mean, wind_2m and solar_rad. Raises if any is missing —
    callers should fall back to hargreaves() rather than guessing.
    """
    if w.rh_mean is None or w.wind_2m is None or w.solar_rad is None:
        raise ValueError("penman_monteith needs rh_mean, wind_2m and solar_rad")

    t = w.t_mean
    delta = slope_vapour_pressure_curve(t)
    gamma = psychrometric_constant(elevation_m)

    # eq. 12: es from Tmax/Tmin, not from Tmean — this matters, it is
    # systematically higher and using Tmean under-estimates ET0.
    es = (saturation_vapour_pressure(w.t_max)
          + saturation_vapour_pressure(w.t_min)) / 2.0
    ea = es * w.rh_mean / 100.0  # eq. 19

    rn = net_radiation(w.solar_rad, w.doy, lat_deg, elevation_m,
                       w.t_max, w.t_min, ea)
    g = 0.0  # daily soil heat flux is negligible, FAO-56 eq. 42

    u2 = max(w.wind_2m, 0.5)  # eq. 6 is unstable below 0.5 m/s

    numerator = 0.408 * delta * (rn - g) + gamma * (900.0 / (t + 273.0)) * u2 * (es - ea)
    denominator = delta + gamma * (1.0 + 0.34 * u2)
    return max(numerator / denominator, 0.0)


def hargreaves(w: DailyWeather, lat_deg: float) -> float:
    """
    ET0 in mm/day. FAO-56 eq. 52. Temperature-only fallback.

    Less accurate than Penman-Monteith — typically within 10-15% in
    semi-arid climates like the Fergana valley. Use only when the weather
    feed is missing humidity or wind.
    """
    ra = extraterrestrial_radiation(w.doy, lat_deg)
    return max(
        0.0023 * (w.t_mean + 17.8) * math.sqrt(max(w.t_max - w.t_min, 0.0)) * ra * 0.408,
        0.0,
    )


def et0(w: DailyWeather, lat_deg: float, elevation_m: float) -> tuple[float, str]:
    """Best available ET0. Returns (mm/day, method used)."""
    try:
        return penman_monteith(w, lat_deg, elevation_m), "penman-monteith"
    except ValueError:
        return hargreaves(w, lat_deg), "hargreaves"
