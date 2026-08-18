"""
Crop coefficients (Kc) and growth stages — FAO-56 chapters 6-7.

Stage lengths and Kc values are FAO-56 Table 11/12 defaults, adjusted to
Uzbek planting calendars. THESE ARE DEFAULTS, NOT MEASUREMENTS. Before
the pilot goes past demo, an agronomist must replace them with values
from the local extension service. Flagged loudly on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Crop:
    key: str
    name_uz: str
    name_ru: str
    # stage lengths in days: initial, development, mid, late
    stages: tuple[int, int, int, int]
    kc_ini: float
    kc_mid: float
    kc_end: float
    root_depth_m: float  # max effective rooting depth
    depletion_fraction: float  # p — fraction of TAW allowed before stress
    typical_sowing: tuple[int, int]  # (month, day) — sowing, or bud break
    perennial: bool = False  # tree/vine: the cycle restarts every spring


# FAO-56 Table 11 (stage lengths), Table 12 (Kc), Table 22 (Zr, p).
CROPS: dict[str, Crop] = {
    "cotton": Crop(
        key="cotton", name_uz="Paxta", name_ru="Хлопок",
        stages=(30, 50, 60, 55),
        kc_ini=0.35, kc_mid=1.15, kc_end=0.60,
        root_depth_m=1.35, depletion_fraction=0.65,
        typical_sowing=(4, 10),
    ),
    "winter_wheat": Crop(
        key="winter_wheat", name_uz="Kuzgi bug'doy", name_ru="Озимая пшеница",
        stages=(30, 140, 40, 30),
        kc_ini=0.40, kc_mid=1.15, kc_end=0.35,
        root_depth_m=1.50, depletion_fraction=0.55,
        typical_sowing=(10, 5),
    ),
    "tomato": Crop(
        key="tomato", name_uz="Pomidor", name_ru="Помидор",
        stages=(30, 40, 45, 30),
        kc_ini=0.60, kc_mid=1.15, kc_end=0.80,
        root_depth_m=1.00, depletion_fraction=0.40,
        typical_sowing=(4, 25),
    ),
    "onion": Crop(
        key="onion", name_uz="Piyoz", name_ru="Лук",
        stages=(20, 35, 110, 45),
        kc_ini=0.70, kc_mid=1.05, kc_end=0.75,
        root_depth_m=0.45, depletion_fraction=0.30,
        typical_sowing=(3, 15),
    ),
    "apple": Crop(
        key="apple", name_uz="Olma", name_ru="Яблоня",
        stages=(30, 50, 130, 30),
        kc_ini=0.45, kc_mid=0.95, kc_end=0.70,
        root_depth_m=1.50, depletion_fraction=0.50,
        typical_sowing=(3, 20),  # bud break, not sowing
        perennial=True,
    ),
    "grape": Crop(
        key="grape", name_uz="Uzum", name_ru="Виноград",
        stages=(20, 50, 75, 60),
        kc_ini=0.30, kc_mid=0.85, kc_end=0.45,
        root_depth_m=1.50, depletion_fraction=0.45,
        typical_sowing=(4, 1),  # bud break
        perennial=True,
    ),
}

STAGE_NAMES_UZ = ("Boshlang'ich", "Rivojlanish", "O'rta", "Yakuniy")
STAGE_NAMES_RU = ("Начальная", "Развитие", "Средняя", "Завершающая")


@dataclass
class StageInfo:
    index: int  # 0..3
    name_uz: str
    name_ru: str
    days_after_planting: int
    kc: float


def season_start(crop: Crop, planting: date, today: date) -> date:
    """
    Начало ТЕКУЩЕГО вегетационного цикла.

    Для однолетних это дата сева. Для многолетних — распускание почек
    этой весны: сад, посаженный девять лет назад, каждый год проходит
    цикл заново, и считать «дни от посадки» бессмысленно.

    Ошибка настоящая, не гипотетическая: виноградник 2017 года давал
    Kc 0.45 вместо 0.85 — почти двукратное занижение потребности поля в
    середине августа, то есть совет поливать вдвое реже, чем нужно.
    """
    if not crop.perennial:
        return planting

    month, day = crop.typical_sowing
    this_year = date(today.year, month, day)
    if today >= this_year:
        return this_year
    return date(today.year - 1, month, day)


def days_after_planting(planting: date, today: date) -> int:
    return (today - planting).days


def stage_and_kc(crop: Crop, dap: int) -> StageInfo:
    """
    Kc for a given day after planting. FAO-56 fig. 25: flat during
    initial and mid, linear interpolation through development and late.
    """
    ini, dev, mid, late = crop.stages
    dap = max(dap, 0)

    if dap < ini:
        kc, idx = crop.kc_ini, 0
    elif dap < ini + dev:
        frac = (dap - ini) / dev
        kc, idx = crop.kc_ini + frac * (crop.kc_mid - crop.kc_ini), 1
    elif dap < ini + dev + mid:
        kc, idx = crop.kc_mid, 2
    elif dap < ini + dev + mid + late:
        frac = (dap - ini - dev - mid) / late
        kc, idx = crop.kc_mid + frac * (crop.kc_end - crop.kc_mid), 3
    else:
        kc, idx = crop.kc_end, 3

    return StageInfo(
        index=idx,
        name_uz=STAGE_NAMES_UZ[idx],
        name_ru=STAGE_NAMES_RU[idx],
        days_after_planting=dap,
        kc=round(kc, 3),
    )


def root_depth(crop: Crop, dap: int) -> float:
    """
    Effective rooting depth grows linearly to its maximum by the end of
    the development stage, then holds. FAO-56 eq. 8-1 simplified.
    """
    ini, dev, _, _ = crop.stages
    z_min = 0.20  # m, at emergence
    if dap >= ini + dev:
        return crop.root_depth_m
    frac = min(max(dap / (ini + dev), 0.0), 1.0)
    return z_min + frac * (crop.root_depth_m - z_min)


def kc_from_ndvi(ndvi: float, crop: Crop) -> float:
    """
    Kc estimated directly from satellite NDVI.

    Linear NDVI->Kcb relation, widely used (Calera et al. 2017):
        Kcb = 1.44 * NDVI - 0.10
    Clamped into the crop's own Kc envelope so a bad pixel cannot produce
    a nonsense recommendation.

    This is the layer that makes the system per-field rather than
    per-district: the crop calendar says what SHOULD be happening, the
    satellite says what IS happening.
    """
    # Clamp against the UNROUNDED envelope, then round for display.
    # Rounding the bounds themselves can push the result a hair past the
    # limit, which quietly defeats the guard.
    kcb = 1.44 * ndvi - 0.10
    lo, hi = crop.kc_ini * 0.6, crop.kc_mid * 1.05
    return round(min(max(kcb, lo), hi), 4)


def blended_kc(calendar_kc: float, ndvi_kc: float | None,
               ndvi_age_days: int = 0) -> tuple[float, str]:
    """
    Combine calendar Kc with satellite Kc.

    Fresh imagery (<= 5 days) is trusted at 70%. Older imagery decays to
    zero weight at 14 days, because a two-week-old NDVI in the middle of
    rapid canopy development is worse than the calendar.
    """
    if ndvi_kc is None:
        return calendar_kc, "calendar"
    if ndvi_age_days > 14:
        return calendar_kc, "calendar (imagery stale)"

    # Возраст бывает и отрицательным: warm-start гонит симуляцию по дням
    # ДО даты снимка, и без ограничения сверху вес превышал 100% —
    # blended_kc(0.95, 0.55, -11) давал 0.45, ниже ОБОИХ источников.
    # Снимок из будущего относительно моделируемого дня весит как свежий.
    weight = 0.70 * max(0.0, min(1.0, 1.0 - ndvi_age_days / 14.0))
    kc = calendar_kc * (1 - weight) + ndvi_kc * weight
    return round(kc, 3), f"calendar+satellite ({int(weight * 100)}% satellite)"
