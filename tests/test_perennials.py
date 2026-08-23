"""
Многолетники: корни не отрастают заново каждую весну, а NDVI сада
переводится в Kc по доле покрытия, а не по формуле для пшеницы.

Две находки аудита 18.08.2026 (crop.py:155 и связка NDVI->Kc), закрытые
23.08.2026 вместе с долей смачивания капли.
"""

from datetime import date

import pytest

from suv.crop import (CROPS, PERENNIAL_ESTABLISHED_YEARS, fraction_cover,
                      kc_from_ndvi, root_depth)
from suv.et0 import DailyWeather
from suv.schedule import Field, simulate
from suv.soil import SOILS, WaterBalanceState


def _wx(doy0, days, t_max=30.0):
    return [DailyWeather(doy=doy0 + i, t_max=t_max, t_min=t_max - 14, rh_mean=35.0,
                         wind_2m=2.0, solar_rad=22.0, rainfall=0.0)
            for i in range(days)]


def _orchard(**kw):
    base = dict(field_id="T-OLMA", name="Olmazor", hectares=2.52,
                lat=39.558, lon=66.996, elevation_m=700.0,
                crop=CROPS["apple"], soil=SOILS["sandy_loam"],
                planting_date=date(2018, 3, 20), irrigation_method="drip",
                water_table_depth_m=40.0)
    base.update(kw)
    return Field(**base)


# ---------------------------------------------------------------- корни

def test_established_orchard_keeps_full_roots_in_spring():
    apple = CROPS["apple"]
    # Неделя после распускания почек: раньше здесь было 0,2-0,5 м.
    assert root_depth(apple, 7, years_since_planting=8.4) == apple.root_depth_m
    assert root_depth(apple, 0, years_since_planting=8.4) == apple.root_depth_m
    # Возраст неизвестен — взрослый сад, а не саженец.
    assert root_depth(apple, 7) == apple.root_depth_m


def test_young_planting_roots_scale_with_sapling_age():
    apple = CROPS["apple"]
    one = root_depth(apple, 100, years_since_planting=1.0)
    two = root_depth(apple, 100, years_since_planting=2.0)
    assert 0.4 <= one < two < apple.root_depth_m
    assert root_depth(apple, 100, PERENNIAL_ESTABLISHED_YEARS) == apple.root_depth_m


def test_annual_root_growth_unchanged():
    cotton = CROPS["cotton"]
    assert root_depth(cotton, 0) == pytest.approx(0.20)
    assert root_depth(cotton, 0) < root_depth(cotton, 40) < root_depth(cotton, 79)
    assert root_depth(cotton, 200) == cotton.root_depth_m
    # Возраст однолетнику не передаётся и ни на что не влияет.
    assert root_depth(cotton, 40, years_since_planting=5.0) == root_depth(cotton, 40)


def test_orchard_taw_does_not_collapse_at_bud_break():
    """Симуляция через распускание почек: запас влаги тот же, что летом,
    и порог весной не в три раза ниже — совет не сыплется каждые два дня."""
    f = _orchard()
    # 15 марта -> 15 апреля: перешагиваем старт сезона 20.03.
    plan = simulate(f, _wx(74, 31, t_max=18.0), WaterBalanceState(30.0, 1.5),
                    date(2026, 3, 15), apply_irrigation=False)
    taws = {round(p.taw_mm) for p in plan}
    assert taws == {78}, taws           # 195 мм * 0,40 смачивания, без скачков
    assert all(p.root_depth_m == pytest.approx(1.5) for p in plan)
    # Дефицит накапливается, а не стирается «ростом корней» на старте.
    assert plan[-1].depletion_mm > plan[0].depletion_mm


# ------------------------------------------------------------- NDVI -> Kc

def test_fraction_cover_is_bounded():
    assert fraction_cover(0.0) == 0.0
    assert fraction_cover(0.15) == 0.0
    assert fraction_cover(0.85) == 1.0
    assert fraction_cover(0.95) == 1.0
    assert 0.4 < fraction_cover(0.47) < 0.5


def test_orchard_ndvi_reads_higher_than_the_herbaceous_line():
    """NDVI 0,47 над кроной с голым междурядьем — не «полпашни под паром»."""
    apple = CROPS["apple"]
    linear = 1.44 * 0.47 - 0.10
    cover = kc_from_ndvi(0.47, apple)
    assert cover > linear + 0.10, (cover, linear)
    assert 0.70 <= cover <= 0.76
    # Сомкнутый полог упирается в тот же конверт, что и раньше.
    assert kc_from_ndvi(0.95, apple) <= apple.kc_mid * 1.05 + 1e-4  # округление до 4 знаков
    assert kc_from_ndvi(0.02, apple) >= apple.kc_ini * 0.6 - 1e-4


def test_orchard_kc_is_monotonic_in_ndvi():
    apple = CROPS["apple"]
    vals = [kc_from_ndvi(n / 100, apple) for n in range(10, 95, 5)]
    assert vals == sorted(vals)


def test_vineyard_keeps_the_validated_linear_relation():
    """Линия 1,44·NDVI−0,10 выведена на виноградниках (Campos et al. 2010) —
    лозе её и оставляем; модель по покрытию — только деревьям."""
    grape = CROPS["grape"]
    assert grape.ndvi_kc_model == "linear"
    assert kc_from_ndvi(0.47, grape) == pytest.approx(1.44 * 0.47 - 0.10, abs=1e-3)
    cotton = CROPS["cotton"]
    assert cotton.ndvi_kc_model == "linear"
    assert kc_from_ndvi(0.47, cotton) == pytest.approx(1.44 * 0.47 - 0.10, abs=1e-3)
