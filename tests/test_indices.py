"""
MSAVI в модели Kc по покрытию + ряды индексов для ретро-разбора.

MSAVI гасит вклад яркости почвы в «долю кроны»: тёмная мокрая земля
после полива не рисует прирост полога, светлая сухая не стирает его.
NDRE добавлен в инструментарий разбора (suv/indices.py) — на плотном
пологе NDVI насыщается, red-edge различает стресс дольше.
"""

from datetime import date

import numpy as np
import pytest

from suv.crop import (CROPS, MSAVI_BARE_SOIL, MSAVI_FULL_COVER,
                      fraction_cover, kc_from_ndvi)
from suv.et0 import DailyWeather
from suv.satellite import EVALSCRIPT, NdviReading, reduce_index_arrays
from suv.schedule import Field, simulate
from suv.soil import SOILS, WaterBalanceState


# ---------------------------------------------------------- доля покрытия

def test_fraction_cover_prefers_msavi_when_present():
    # Мокрая тёмная почва задирает NDVI без листа: MSAVI держит долю.
    assert fraction_cover(0.35, msavi=0.20) == pytest.approx(
        (0.20 - MSAVI_BARE_SOIL) / (MSAVI_FULL_COVER - MSAVI_BARE_SOIL))
    # Без MSAVI — прежняя шкала NDVI, ничего не поехало.
    assert fraction_cover(0.35) == pytest.approx((0.35 - 0.15) / 0.70)


def test_fraction_cover_msavi_is_bounded():
    assert fraction_cover(0.9, msavi=0.05) == 0.0
    assert fraction_cover(0.2, msavi=0.90) == 1.0


def test_orchard_kc_uses_msavi_only_for_cover_model():
    apple, grape = CROPS["apple"], CROPS["grape"]
    with_m = kc_from_ndvi(0.47, apple, msavi=0.30)
    without = kc_from_ndvi(0.47, apple)
    assert with_m != without          # сад: MSAVI меняет долю кроны
    # Линейная модель (лоза, однолетние) MSAVI не использует.
    assert kc_from_ndvi(0.47, grape, msavi=0.30) == kc_from_ndvi(0.47, grape)


def test_wet_soil_after_irrigation_does_not_inflate_orchard_kc():
    """Сценарий, ради которого MSAVI и добавлен: после полива мокрое
    междурядье поднимает NDVI, но не MSAVI — Kc не должен подскакивать."""
    apple = CROPS["apple"]
    dry = kc_from_ndvi(0.44, apple, msavi=0.30)     # до полива
    wet = kc_from_ndvi(0.50, apple, msavi=0.31)     # после: NDVI +0.06 от почвы
    assert abs(wet - dry) < 0.03, (dry, wet)
    # Тот же скачок без MSAVI сдвинул бы Kc заметно сильнее.
    assert abs(kc_from_ndvi(0.50, apple) - kc_from_ndvi(0.44, apple)) > 0.05


def test_advice_deliberately_ignores_msavi_for_now():
    """Решение 09.09.2026: живой замер дал fc(MSAVI) 0,30 против 0,42 по
    NDVI — минус 22% Kc без полевой правды. Совет остаётся на NDVI-шкале
    (она сверена с каденсом фермера); MSAVI — данные и разбор. Этот тест
    прибивает решение: если кто-то включит MSAVI в расчёт, пусть сделает
    это сознательно, вместе с калибровкой доли кроны."""
    wx = [DailyWeather(doy=230 + i, t_max=34.0, t_min=20.0, rh_mean=30.0,
                       wind_2m=2.0, solar_rad=23.0, rainfall=0.0)
          for i in range(3)]
    base = dict(field_id="T", name="T", hectares=2.52, lat=39.56, lon=67.0,
                elevation_m=700.0, crop=CROPS["apple"],
                soil=SOILS["sandy_loam"], planting_date=date(2018, 3, 20),
                irrigation_method="drip", water_table_depth_m=40.0,
                ndvi=0.50, ndvi_date=date(2026, 8, 28))
    st = WaterBalanceState(10.0, 1.5)
    kc_plain = simulate(Field(**base), wx, st, date(2026, 8, 30))[0].kc
    kc_msavi = simulate(Field(**base, msavi=0.30), wx, st, date(2026, 8, 30))[0].kc
    assert kc_msavi == kc_plain


# ------------------------------------------------------- редукция кадров

def _arrays(clear_n=8, cloud_n=2, outside_n=6):
    clear = np.array([True] * clear_n + [False] * (cloud_n + outside_n))
    inside = np.array([True] * (clear_n + cloud_n) + [False] * outside_n)
    idx = np.linspace(0.2, 0.6, clear_n + cloud_n + outside_n).astype("float32")
    return clear, inside, idx


def test_reduce_counts_cloud_fraction_inside_the_field_only():
    clear, inside, idx = _arrays()
    got = reduce_index_arrays(clear, inside, idx, idx * 2)
    assert got is not None
    mean1, mean2, frac = got
    assert frac == pytest.approx(0.8)             # 8 чистых из 10 В ПОЛЕ
    assert mean1 == pytest.approx(float(idx[clear].mean()))
    assert mean2 == pytest.approx(2 * mean1)


def test_reduce_refuses_blind_frames():
    clear, inside, idx = _arrays(clear_n=2, cloud_n=8)   # 20% чистых
    assert reduce_index_arrays(clear, inside, idx) is None
    empty = np.zeros(4, dtype=bool)
    assert reduce_index_arrays(empty, empty, np.zeros(4)) is None


def test_ndvi_reading_carries_msavi_and_defaults_to_none():
    r = NdviReading(value=0.5, observed_on=date(2026, 9, 1),
                    valid_fraction=1.0)
    assert r.msavi is None            # Landsat-резерв конструирует без MSAVI
    assert EVALSCRIPT.count("msavi") >= 2 and '"bands": 4' not in EVALSCRIPT
    assert "output: {bands: 4" in EVALSCRIPT


# ---------------------------------------------------------- ряд индексов

def test_index_eval_computes_all_four_indices():
    from suv.indices import INDEX_EVAL
    for band in ("B04", "B05", "B08", "B11", "SCL"):
        assert band in INDEX_EVAL
    assert "output: {bands: 6" in INDEX_EVAL
    for name in ("ndvi", "msavi", "ndre", "ndmi"):
        assert name in INDEX_EVAL


def test_index_reading_fields():
    from suv.indices import IndexReading
    r = IndexReading(day=date(2025, 7, 19), ndvi=0.42, msavi=0.30,
                     ndre=0.25, ndmi=0.14, valid_fraction=0.97)
    assert r.day.isoformat() == "2025-07-19"
    assert r.ndmi < r.ndre < r.ndvi   # подпись стресса из разбора-2025
