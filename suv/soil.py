"""
Soil water balance — FAO-56 chapter 8.

Tracks root-zone depletion day by day and decides when the field crosses
the stress threshold. This is where the water saving actually comes from:
the classic Uzbek practice is a fixed interval regardless of what the
soil holds, and a fixed interval is wrong on both ends — it over-waters
in cool weeks and under-waters in heatwaves.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SoilType:
    key: str
    name_ru: str
    field_capacity: float  # m3/m3
    wilting_point: float  # m3/m3
    infiltration_mm_h: float

    @property
    def available_water_mm_per_m(self) -> float:
        """TAW per metre of root depth. FAO-56 eq. 82."""
        return 1000.0 * (self.field_capacity - self.wilting_point)


# FAO-56 Table 19. Fergana valley is dominated by loam and silt loam;
# Bukhara/Karakalpakstan skew sandier and more saline.
SOILS: dict[str, SoilType] = {
    "sand": SoilType("sand", "Песчаная", 0.15, 0.07, 50.0),
    "sandy_loam": SoilType("sandy_loam", "Супесчаная", 0.23, 0.10, 25.0),
    "loam": SoilType("loam", "Суглинистая", 0.32, 0.15, 13.0),
    "silt_loam": SoilType("silt_loam", "Пылевато-суглинистая", 0.33, 0.15, 8.0),
    "clay_loam": SoilType("clay_loam", "Глинисто-суглинистая", 0.36, 0.20, 4.0),
    "clay": SoilType("clay", "Глинистая", 0.40, 0.24, 2.0),
}

# Maximum NET depth a single irrigation event can usefully deliver, mm.
# A furrow pass cannot infiltrate 200 mm — most of it runs off the tail or
# percolates below the roots. Without this cap the engine produces two
# enormous irrigations per season, which is not how any farm operates and
# would immediately expose the model as untested in front of an agronomist.
MAX_APPLICATION_MM = {
    "furrow": 100.0,
    "furrow_improved": 90.0,
    "sprinkler": 50.0,
    "drip": 25.0,
}

# Application efficiency by irrigation method. The gap between furrow and
# drip is exactly the 60 trillion soum the state is spending — but note
# that scheduling savings sit ON TOP of these, because efficiency only
# describes losses during application, not applying on the wrong day.
IRRIGATION_EFFICIENCY = {
    "furrow": 0.55,
    "furrow_improved": 0.70,
    "sprinkler": 0.75,
    "drip": 0.90,
}


def capillary_rise(water_table_depth_m: float, root_depth_m: float,
                   etc_mm_day: float) -> float:
    """
    Upward flux from a shallow water table into the root zone, mm/day.
    The CR term of the FAO-56 water balance (eq. 85), usually dropped by
    textbook implementations because most of the world has a deep table.

    Uzbekistan does not. Across much of the irrigated Fergana and
    Zarafshan valleys the table sits 1-3 m down, and it supplies a large
    share of crop demand. Ignoring this term makes the model demand
    roughly twice the water that farms actually apply — we found that the
    hard way, and the test suite now locks it in.

    The same shallow table is the mechanism behind the salinity damage on
    half the country's irrigated land: water rising through the profile
    carries salt up with it and leaves it at the surface when it
    evaporates. So this term is not only a correction, it is the variable
    the salinity warning is built on.

    Simplified from Doorenbos & Pruitt / FAO-24 contribution tables:
    full contribution when the table is within reach of the roots,
    decaying to zero about 2 m below the root zone.
    """
    if water_table_depth_m <= 0:
        return 0.0
    gap = water_table_depth_m - root_depth_m
    if gap <= 0:
        frac = 0.60          # table inside the root zone: waterlogging risk
    elif gap >= 2.0:
        frac = 0.0           # too deep to contribute
    else:
        frac = 0.60 * (1.0 - gap / 2.0)
    return max(0.0, frac * etc_mm_day)


def salinity_risk(water_table_depth_m: float, percolation_mm: float) -> str:
    """
    Plain-language salinity flag. A high table plus heavy percolation is
    the combination that lifts salt into the topsoil.
    """
    if water_table_depth_m <= 0:
        return "unknown"
    if water_table_depth_m < 1.5 and percolation_mm > 20:
        return "high"
    if water_table_depth_m < 2.0 or percolation_mm > 40:
        return "moderate"
    return "low"


@dataclass
class WaterBalanceState:
    """Root-zone depletion in mm. 0 = at field capacity."""

    depletion_mm: float = 0.0
    last_root_depth_m: float = 0.20


def total_available_water(soil: SoilType, root_depth_m: float) -> float:
    """TAW, mm. FAO-56 eq. 82."""
    return soil.available_water_mm_per_m * root_depth_m


def readily_available_water(taw: float, depletion_fraction: float,
                            etc_mm_day: float) -> float:
    """
    RAW, mm. FAO-56 eq. 83, with the eq. 84 adjustment for ET rate:
    the allowable depletion shrinks on high-demand days.
    """
    p_adj = depletion_fraction + 0.04 * (5.0 - etc_mm_day)
    p_adj = min(max(p_adj, 0.10), 0.80)
    return taw * p_adj


def effective_rainfall(rain_mm: float) -> float:
    """
    USDA-SCS style: small showers evaporate before reaching the root
    zone, heavy rain partly runs off. Deliberately conservative — telling
    a farmer to skip an irrigation because of rain that did not soak in
    is the fastest way to lose his trust.
    """
    if rain_mm < 3.0:
        return 0.0
    if rain_mm <= 25.0:
        return rain_mm * 0.85
    return 25.0 * 0.85 + (rain_mm - 25.0) * 0.55


def step(state: WaterBalanceState, soil: SoilType, root_depth_m: float,
         etc_mm: float, rain_mm: float, irrigation_mm: float = 0.0,
         water_table_depth_m: float = 0.0
         ) -> tuple[WaterBalanceState, float]:
    """
    Advance the balance one day. FAO-56 eq. 85.

    Returns the new state and deep percolation (mm) — water pushed below
    the root zone. On saline land this number is not merely wasted: it is
    what raises the water table and brings salt up. Worth surfacing to
    the farmer, not hiding.
    """
    taw = total_available_water(soil, root_depth_m)

    # Root growth reaches into soil that is already at field capacity, so
    # deepening the zone dilutes existing depletion rather than adding to it.
    if root_depth_m > state.last_root_depth_m:
        state.depletion_mm *= state.last_root_depth_m / root_depth_m

    p_eff = effective_rainfall(rain_mm)
    cr = capillary_rise(water_table_depth_m, root_depth_m, etc_mm)
    depletion = state.depletion_mm - p_eff - irrigation_mm - cr + etc_mm

    percolation = 0.0
    if depletion < 0.0:
        percolation = -depletion
        depletion = 0.0
    depletion = min(depletion, taw)

    return WaterBalanceState(depletion, root_depth_m), percolation


def net_irrigation_requirement(state: WaterBalanceState, soil: SoilType,
                               root_depth_m: float) -> float:
    """Millimetres needed to refill the root zone to field capacity."""
    return max(0.0, min(state.depletion_mm,
                        total_available_water(soil, root_depth_m)))


def gross_irrigation(net_mm: float, method: str) -> float:
    """What actually has to be delivered at the head of the field."""
    eff = IRRIGATION_EFFICIENCY.get(method, 0.55)
    return net_mm / eff


def mm_to_m3(mm: float, hectares: float) -> float:
    """1 mm over 1 ha = 10 m3."""
    return mm * 10.0 * hectares
