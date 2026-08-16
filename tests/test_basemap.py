"""
Математика тайлов Web Mercator для архивной подложки.

Ошибка здесь — это «поле уехало на соседний квартал»: заливка влажности
легла бы на чужой участок при идеально работающей сети. Поэтому вся
геометрия проверяется без интернета.
"""

from __future__ import annotations

import pytest

from suv.basemap import (MAX_ZOOM, MIN_ZOOM, TILE_PX, crop_window,
                         mercator_px, pick_zoom, tile_range)

# Виноградник Фарруха: bbox примерно 250x210 м.
BOX = (66.99565, 39.55751, 66.99827, 39.55953)


def test_mercator_anchors():
    """Опорные точки проекции: центр мира и датумы краёв."""
    x, y = mercator_px(0.0, 0.0, 0)
    assert (x, y) == pytest.approx((128.0, 128.0))

    x, _ = mercator_px(-180.0, 0.0, 0)
    assert x == pytest.approx(0.0)
    x, _ = mercator_px(180.0, 0.0, 0)
    assert x == pytest.approx(256.0)

    # Севернее — МЕНЬШЕ y: у веб-меркатора ось растёт вниз.
    _, y_north = mercator_px(66.9, 39.66, 15)
    _, y_south = mercator_px(66.9, 39.55, 15)
    assert y_north < y_south


def test_zoom_doubles_the_scale():
    x1, y1 = mercator_px(66.9976, 39.5585, 16)
    x2, y2 = mercator_px(66.9976, 39.5585, 17)
    assert x2 == pytest.approx(2 * x1)
    assert y2 == pytest.approx(2 * y1)


def test_pick_zoom_gives_a_reasonable_frame():
    """Кадр не шире просимого и не мельче минимального зума."""
    z = pick_zoom(BOX, want_px=1000)
    assert MIN_ZOOM <= z <= MAX_ZOOM
    x0, _ = mercator_px(BOX[0], BOX[1], z)
    x1, _ = mercator_px(BOX[2], BOX[3], z)
    assert abs(x1 - x0) <= 1000
    # На зум выше кадр уже не влез бы — то есть выбран максимально чёткий.
    if z < MAX_ZOOM:
        xa, _ = mercator_px(BOX[0], BOX[1], z + 1)
        xb, _ = mercator_px(BOX[2], BOX[3], z + 1)
        assert abs(xb - xa) > 1000


def test_crop_window_sits_inside_the_tile_sheet():
    """Окно вырезки обязано лежать внутри склеенного полотна тайлов —
    иначе crop молча дополнит кадр чёрным, и поле сместится."""
    for z in (MIN_ZOOM, 17, MAX_ZOOM):
        tx0, ty0, tx1, ty1 = tile_range(BOX, z)
        assert tx1 >= tx0 and ty1 >= ty0
        left, top, right, bottom = crop_window(BOX, z, tx0, ty0)
        sheet_w = (tx1 - tx0 + 1) * TILE_PX
        sheet_h = (ty1 - ty0 + 1) * TILE_PX
        assert 0 <= left < right <= sheet_w
        assert 0 <= top < bottom <= sheet_h


def test_crop_matches_the_bbox_extent():
    """Размер вырезки равен протяжённости bbox в пикселях зума."""
    z = 18
    tx0, ty0, _tx1, _ty1 = tile_range(BOX, z)
    left, top, right, bottom = crop_window(BOX, z, tx0, ty0)
    x0, y1 = mercator_px(BOX[0], BOX[1], z)
    x1, y0 = mercator_px(BOX[2], BOX[3], z)
    assert (right - left) == pytest.approx(x1 - x0, abs=1.0)
    assert (bottom - top) == pytest.approx(y1 - y0, abs=1.0)
