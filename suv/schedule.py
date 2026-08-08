"""
The decision engine.

Everything else in this package computes numbers. This module turns the
numbers into one sentence a farmer can act on: irrigate on this day, this
many millimetres, this field.

Design rule: never output a table. A farmer with a 5-inch screen and a
tractor to drive gets one instruction and one reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .crop import (Crop, blended_kc, kc_from_ndvi, root_depth, season_start,
                   stage_and_kc)
from .et0 import DailyWeather, et0
from .soil import (
    MAX_APPLICATION_MM,
    SoilType,
    salinity_risk,
    WaterBalanceState,
    gross_irrigation,
    mm_to_m3,
    net_irrigation_requirement,
    readily_available_water,
    step,
    total_available_water,
)


@dataclass
class Field:
    field_id: str
    name: str
    hectares: float
    lat: float
    lon: float
    elevation_m: float
    crop: Crop
    soil: SoilType
    planting_date: date
    irrigation_method: str = "furrow"
    water_table_depth_m: float = 0.0  # 0 = deep/unknown, no contribution
    ndvi: float | None = None
    ndvi_date: date | None = None


@dataclass
class DayPlan:
    day: date
    etc_mm: float
    et0_mm: float
    kc: float
    kc_source: str
    rain_mm: float
    depletion_mm: float
    taw_mm: float
    raw_mm: float
    stress: bool
    irrigate: bool
    salinity: str = "unknown"
    net_mm: float = 0.0
    gross_mm: float = 0.0
    gross_m3: float = 0.0
    percolation_mm: float = 0.0


@dataclass
class Recommendation:
    field: Field
    generated_on: date
    action_day: date | None
    gross_mm: float
    gross_m3: float
    reason_key: str
    days_until: int
    plan: list[DayPlan] = field(default_factory=list)
    baseline_m3: float = 0.0
    saved_m3: float = 0.0


def simulate(
    fld: Field,
    forecast: list[DailyWeather],
    start_state: WaterBalanceState,
    start_day: date,
    apply_irrigation: bool = True,
) -> list[DayPlan]:
    """
    Roll the water balance forward across the forecast window.

    When apply_irrigation is True the engine irrigates the moment
    depletion crosses RAW — that is the recommendation. When False it
    lets the field run dry, which is only used for diagnostics.
    """
    state = WaterBalanceState(start_state.depletion_mm, start_state.last_root_depth_m)
    plan: list[DayPlan] = []

    for i, w in enumerate(forecast):
        day = start_day + timedelta(days=i)
        # Многолетники считаются от распускания почек этого года, а не от
        # года посадки — иначе сад «стареет» на десятилетия внутри модели.
        dap = (day - season_start(fld.crop, fld.planting_date, day)).days

        stage = stage_and_kc(fld.crop, dap)
        zr = root_depth(fld.crop, dap)

        ndvi_kc = None
        ndvi_age = 99
        if fld.ndvi is not None and fld.ndvi_date is not None:
            ndvi_kc = kc_from_ndvi(fld.ndvi, fld.crop)
            ndvi_age = (day - fld.ndvi_date).days
        kc, kc_source = blended_kc(stage.kc, ndvi_kc, ndvi_age)

        ref, _method = et0(w, fld.lat, fld.elevation_m)
        etc = ref * kc

        taw = total_available_water(fld.soil, zr)
        raw = readily_available_water(taw, fld.crop.depletion_fraction, etc)

        # Decide before applying the day's ET: the farmer acts in the
        # morning on yesterday's state, not on tonight's.
        irrigate = apply_irrigation and state.depletion_mm >= raw
        net = gross = m3 = 0.0
        if irrigate:
            net = net_irrigation_requirement(state, fld.soil, zr)
            net = min(net, MAX_APPLICATION_MM.get(fld.irrigation_method, 100.0))
            gross = gross_irrigation(net, fld.irrigation_method)
            m3 = mm_to_m3(gross, fld.hectares)

        state, perc = step(state, fld.soil, zr, etc, w.rainfall, net,
                           fld.water_table_depth_m)

        plan.append(DayPlan(
            day=day, etc_mm=round(etc, 2), et0_mm=round(ref, 2),
            kc=kc, kc_source=kc_source, rain_mm=w.rainfall,
            depletion_mm=round(state.depletion_mm, 1),
            taw_mm=round(taw, 1), raw_mm=round(raw, 1),
            stress=state.depletion_mm > raw,
            irrigate=irrigate, net_mm=round(net, 1),
            gross_mm=round(gross, 1), gross_m3=round(m3, 1),
            percolation_mm=round(perc, 1),
            salinity=salinity_risk(fld.water_table_depth_m, perc),
        ))

    return plan


def fixed_interval_baseline(
    fld: Field,
    forecast: list[DailyWeather],
    start_state: WaterBalanceState,
    start_day: date,
    interval_days: int = 30,
    application_m3_per_ha: float = 1100.0,
) -> float:
    """
    What the farmer would have used on the traditional calendar: a fixed
    gross application every N days regardless of soil, weather or stage.

    Defaults (10 days / 90 mm gross furrow) are typical Fergana practice
    but MUST be replaced with what the specific pilot farmer actually did
    last season. The saving figure is only credible if the baseline is his
    own behaviour, not a national average.
    """
    origin = season_start(fld.crop, fld.planting_date, start_day)
    n = sum(1 for i in range(len(forecast))
            if (start_day + timedelta(days=i) - origin).days % interval_days == 0)
    return application_m3_per_ha * n * fld.hectares


def recommend(
    fld: Field,
    forecast: list[DailyWeather],
    start_state: WaterBalanceState,
    today: date,
    baseline_interval_days: int = 30,
    baseline_application_m3_per_ha: float = 1100.0,
) -> Recommendation:
    """Produce the single instruction that goes out over Telegram."""
    plan = simulate(fld, forecast, start_state, today)

    action = next((p for p in plan if p.irrigate), None)
    scheduled_m3 = sum(p.gross_m3 for p in plan)
    baseline_m3 = fixed_interval_baseline(
        fld, forecast, start_state, today,
        baseline_interval_days, baseline_application_m3_per_ha,
    )

    if action is None:
        rain_ahead = sum(p.rain_mm for p in plan)
        reason = "rain_expected" if rain_ahead >= 10 else "soil_still_wet"
        return Recommendation(
            field=fld, generated_on=today, action_day=None,
            gross_mm=0.0, gross_m3=0.0, reason_key=reason, days_until=-1,
            plan=plan, baseline_m3=round(baseline_m3, 1),
            saved_m3=round(baseline_m3 - scheduled_m3, 1),
        )

    days_until = (action.day - today).days
    if days_until == 0:
        reason = "threshold_reached"
    elif any(p.rain_mm >= 5 for p in plan[:days_until]):
        reason = "after_rain"
    else:
        reason = "threshold_approaching"

    return Recommendation(
        field=fld, generated_on=today, action_day=action.day,
        gross_mm=action.gross_mm, gross_m3=action.gross_m3,
        reason_key=reason, days_until=days_until, plan=plan,
        baseline_m3=round(baseline_m3, 1),
        saved_m3=round(baseline_m3 - scheduled_m3, 1),
    )
