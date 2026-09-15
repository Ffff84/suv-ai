"""
Уклон поля по GLO-30: вписанная плоскость и её правила честности.

Проверяется чистая математика (fit_plane, румбы, проверка самотёка) —
сетевую часть, как и у satellite.py, доказывает только прогон
scripts/field_slope.py с живыми ключами. Главные инварианты: известная
плоскость восстанавливается точно, шум без уклона не выдаётся за уклон
(порог 2·SE), поле размера Uzumzor с уклоном 0,2% честно получает
«неотличим», а не уверенную цифру.
"""

import math

import numpy as np
import pytest

from suv.satellite import bbox_polygon
from suv.terrain import (MIN_PIXELS, SlopeEstimate, fit_plane,
                         gravity_check, points_in_ring, rumb, tile_url)

DX = DY = 30.0


def grid(n_rows: int, n_cols: int, east_per_m: float, north_per_m: float,
         base: float = 500.0, noise: float = 0.0, seed: int = 0):
    """Растр высот с заданным градиентом. Строка 0 — северный край,
    поэтому «на север» = минус номер строки, как в GeoTIFF."""
    rows, cols = np.mgrid[0:n_rows, 0:n_cols]
    z = base + east_per_m * (cols * DX) + north_per_m * (-(rows * DY))
    if noise:
        z = z + np.random.default_rng(seed).normal(0.0, noise, z.shape)
    return z


# ------------------------------------------------------------ плоскость

def test_exact_plane_is_recovered():
    # 0,4% на восток + 0,3% на север = 0,5% вниз на юго-запад-ЮЗЗ.
    z = grid(10, 10, east_per_m=0.004, north_per_m=0.003)
    est = fit_plane(z, np.ones_like(z, bool), DX, DY)
    assert est is not None
    assert est.slope_pct == pytest.approx(0.5, abs=1e-6)
    assert est.se_pct == pytest.approx(0.0, abs=1e-6)
    # Градиент вверх на СВ (53°) -> сток на ЮЗ: 180 + 53.13.
    assert est.aspect_deg == pytest.approx(
        180.0 + math.degrees(math.atan2(0.004, 0.003)), abs=0.1)
    assert est.downhill == "юго-запад"
    assert est.detectable


def test_pure_north_tilt_flows_south():
    # Выше на севере — вода течёт на юг, азимут 180.
    z = grid(8, 8, east_per_m=0.0, north_per_m=0.005)
    est = fit_plane(z, np.ones_like(z, bool), DX, DY)
    assert est.aspect_deg == pytest.approx(180.0, abs=0.1)
    assert est.downhill == "юг"


def test_noisy_plane_keeps_slope_and_reports_se():
    z = grid(10, 10, east_per_m=0.01, north_per_m=0.0, noise=0.5, seed=1)
    est = fit_plane(z, np.ones_like(z, bool), DX, DY)
    # 1% на 300 м — перепад 3 м, шум 0,5 м: уклон виден и величина близка.
    assert est.detectable
    assert est.slope_pct == pytest.approx(1.0, rel=0.25)
    assert est.se_pct > 0.0


def test_flat_noise_is_not_promoted_to_slope():
    """Правило field_photo для рельефа: узор из шума не рисуем."""
    z = grid(10, 10, east_per_m=0.0, north_per_m=0.0, noise=1.5, seed=42)
    est = fit_plane(z, np.ones_like(z, bool), DX, DY)
    assert est is not None
    assert not est.detectable


def test_uzum_size_field_refuses_instead_of_guessing():
    """Поле 120 м — 4x4 = 16 пикселей. Замер показал (см. MIN_PIXELS):
    на такой сетке треть прогонов ЧИСТОГО ШУМА проходит порог 2·SE с
    «уверенными» 2%. Правильный ответ — отказ и совет --expand."""
    z = grid(4, 4, east_per_m=0.002, north_per_m=0.0, noise=1.0, seed=7)
    assert 16 < MIN_PIXELS
    assert fit_plane(z, np.ones_like(z, bool), DX, DY) is None


def test_degenerate_single_column_refuses():
    # Пикселей достаточно, но все в одном столбце: наклон на восток
    # неопределим, матрица вырождена — отказ, а не случайный ответ.
    z = grid(30, 1, east_per_m=0.0, north_per_m=0.005)
    assert fit_plane(z, np.ones_like(z, bool), DX, DY) is None


def test_pixels_outside_mask_are_ignored():
    z = grid(10, 10, east_per_m=0.004, north_per_m=0.003)
    inside = np.ones_like(z, bool)
    # Мусор вне контура (соседняя постройка +100 м) не должен влиять.
    z = z.copy()
    z[0, :3] += 100.0
    inside[0, :3] = False
    est = fit_plane(z, inside, DX, DY)
    assert est.slope_pct == pytest.approx(0.5, abs=1e-6)


def test_perfectly_flat_field():
    z = grid(6, 6, east_per_m=0.0, north_per_m=0.0)
    est = fit_plane(z, np.ones_like(z, bool), DX, DY)
    assert est.slope_pct == pytest.approx(0.0, abs=1e-9)
    assert not est.detectable


# ------------------------------------------------------------------ румбы

def test_rumb_names():
    assert rumb(0) == "север"
    assert rumb(359) == "север"
    assert rumb(45) == "северо-восток"
    assert rumb(225) == "юго-запад"


def test_gravity_check_agrees_and_contradicts():
    # Uzumzor: вода с северо-востока, сток на ЮЗ (225°) — самотёк сходится.
    assert gravity_check("северо-восток", 225.0) is True
    # Сток на СВ при входе с СВ — вода должна была бы течь в горку.
    assert gravity_check("северо-восток", 45.0) is False
    # Полтора румба расхождения ещё прощаем (67,5°), два — уже нет.
    assert gravity_check("север", 225.0) is True
    assert gravity_check("юго-восток", 225.0) is False


def test_gravity_check_unknown_side_is_none():
    assert gravity_check("с торца у арыка", 225.0) is None


def test_tile_url_for_farrukh_fields():
    # Оба поля Фарруха (39.558, 66.996) лежат на листе N39/E066.
    ring = bbox_polygon(39.558108, 66.996099, 200.0)
    assert tile_url(ring).endswith(
        "Copernicus_DSM_COG_10_N39_00_E066_00_DEM.tif")


def test_tile_url_refuses_on_sheet_boundary():
    # Квадрат через целый градус долготы — отказ, а не пол-поля.
    ring = bbox_polygon(39.5, 67.0, 400.0)
    with pytest.raises(RuntimeError, match="стык"):
        tile_url(ring)


def test_points_in_ring_square():
    ring = bbox_polygon(39.558, 66.996, 200.0)
    lon = np.array([66.996, 66.996, 60.0])
    lat = np.array([39.558, 39.60, 39.558])
    got = points_in_ring(lon, lat, ring)
    assert got.tolist() == [True, False, False]


def test_points_in_ring_matches_grid_share():
    # Ромб внутри квадрата 2x2: площадь ровно половина — доля точек
    # мелкой сетки внутри должна быть близка к 0,5.
    ring = [[0.0, -1.0], [1.0, 0.0], [0.0, 1.0], [-1.0, 0.0], [0.0, -1.0]]
    g = np.linspace(-0.999, 0.999, 60)
    lon, lat = np.meshgrid(g, g)
    share = points_in_ring(lon, lat, ring).mean()
    assert share == pytest.approx(0.5, abs=0.03)


def test_detectable_rule_on_dataclass():
    est = SlopeEstimate(slope_pct=0.4, se_pct=0.3, aspect_deg=0.0,
                        n_pixels=25, elev_mean=500.0, elev_span=1.0)
    assert not est.detectable          # 0.4 < 2*0.3
    est2 = SlopeEstimate(slope_pct=0.61, se_pct=0.3, aspect_deg=0.0,
                         n_pixels=25, elev_mean=500.0, elev_span=1.0)
    assert est2.detectable
