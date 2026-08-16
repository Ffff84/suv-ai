"""
Фото поля: правила показа и раскраска.

Главное, что здесь закреплено, — границы честности. Фото не должно
появляться там, где под ним нет данных, а ровное поле не должно
раскрашиваться в красное и зелёное только потому, что шкалу растянули.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from suv.field_photo import (MAX_SCENE_AGE_DAYS, MIN_AREA_HA, MIN_SPREAD,
                             OVERLAY_ALPHA, bbox_of, can_show_photo,
                             moisture_color, ring_to_pixels, scale_bar_m,
                             stats_over_field)

TODAY = date(2026, 8, 15)
FRESH = TODAY - timedelta(days=3)

# Квадрат 3x3: центральная клетка внутри поля, крайние снаружи.
FIELD = [[False, False, False],
         [False, True, True],
         [False, True, True]]
CLEAR = [[True, True, True],
         [True, True, True],
         [True, True, True]]
NDMI = [[0.90, 0.90, 0.90],
        [0.90, 0.10, 0.30],
        [0.90, 0.50, 0.20]]


# ---------------------------------------------------------------- замер

def test_valid_fraction_counts_the_field_not_the_frame():
    """Доля чистых пикселей считается от площади ПОЛЯ.

    Считать от всего кадра — значит мерить, какую часть прямоугольника
    занимает участок: поле без единого облака выходило бы «закрытым на
    треть» и отбраковывалось (ТЗ §4.2).
    """
    st = stats_over_field(NDMI, FIELD, CLEAR)
    assert st.valid_fraction == 1.0
    assert st.pixels == 4


def test_values_outside_the_contour_are_ignored():
    """Соседский участок в кадр попадает, в замер — нет."""
    st = stats_over_field(NDMI, FIELD, CLEAR)
    assert st.low == 0.10 and st.high == 0.50   # 0.90 снаружи не взято
    assert st.mean == pytest.approx((0.10 + 0.30 + 0.50 + 0.20) / 4)


def test_clouded_pixels_lower_the_valid_fraction():
    clouded = [[True, True, True],
               [True, False, False],
               [True, True, True]]
    st = stats_over_field(NDMI, FIELD, clouded)
    assert st.valid_fraction == 0.5             # 2 из 4 клеток поля
    assert st.pixels == 2


def test_no_clear_pixels_means_no_measurement():
    dark = [[False] * 3 for _ in range(3)]
    assert stats_over_field(NDMI, FIELD, dark) is None


def test_one_puddle_does_not_paint_the_whole_field_red():
    """Самая опасная ошибка этой картинки.

    Шкала растягивается на диапазон поля. Если брать крайние значения,
    одной лужи, мокрой колеи или края канала хватает, чтобы задать верх
    шкалы: остальные 289 клеток оказываются у нижнего края и красятся в
    ярко-красное «сухо». Фермер идёт поливать политое — ровно то, о чём
    предупреждает §1.1.
    """
    field = [[True] * 6 for _ in range(6)]
    clear = [[True] * 6 for _ in range(6)]
    flat_with_puddle = [0.30] * 35 + [0.75]
    grid = [flat_with_puddle[i * 6:(i + 1) * 6] for i in range(6)]

    st = stats_over_field(grid, field, clear)
    assert st.wettest == 0.75, "выброс никуда не делся из данных"
    assert st.high == pytest.approx(0.30), "но шкалу он не задаёт"
    assert st.uniform, "поле ровное — красить его нельзя"

    dry_patch = [0.45] * 35 + [0.02]
    st2 = stats_over_field([dry_patch[i * 6:(i + 1) * 6] for i in range(6)],
                           field, clear)
    assert st2.driest == 0.02 and st2.uniform


def test_a_real_gradient_is_still_painted():
    """Устойчивая шкала не должна съедать настоящий перепад."""
    field = [[True] * 6 for _ in range(6)]
    clear = [[True] * 6 for _ in range(6)]
    ramp = [0.10 + 0.02 * i for i in range(36)]
    st = stats_over_field([ramp[i * 6:(i + 1) * 6] for i in range(6)],
                          field, clear)
    assert not st.uniform and st.spread > 0.5
    r_dry, g_dry, *_ = moisture_color(st.low, st)
    r_wet, g_wet, *_ = moisture_color(st.high, st)
    assert r_dry > g_dry and g_wet > r_wet


def test_even_field_is_reported_as_uniform():
    """Разброс меньше порога — говорить о сухих и влажных зонах нельзя."""
    flat = [[0.40, 0.40, 0.40],
            [0.40, 0.40, 0.42],
            [0.40, 0.41, 0.40]]
    st = stats_over_field(flat, FIELD, CLEAR)
    assert st.spread < MIN_SPREAD
    assert st.uniform

    st2 = stats_over_field(NDMI, FIELD, CLEAR)
    assert not st2.uniform


# ---------------------------------------------------------------- цвет

def test_dry_is_red_wet_is_green():
    st = stats_over_field(NDMI, FIELD, CLEAR)
    r_dry, g_dry, _b, _a = moisture_color(st.low, st)
    r_wet, g_wet, _b2, _a2 = moisture_color(st.high, st)
    assert r_dry > g_dry, "самое сухое место должно быть красным"
    assert g_wet > r_wet, "самое влажное — зелёным"


def test_dry_zone_is_painted_denser():
    """Красный на тёмной пашне теряется, поэтому сухая зона плотнее."""
    st = stats_over_field(NDMI, FIELD, CLEAR)
    *_rgb, a_dry = moisture_color(st.low, st)
    *_rgb2, a_wet = moisture_color(st.high, st)
    assert a_dry > a_wet
    assert a_wet >= int(255 * OVERLAY_ALPHA) - 1


def test_colour_scale_is_clamped_outside_the_measured_range():
    st = stats_over_field(NDMI, FIELD, CLEAR)
    assert moisture_color(-5.0, st) == moisture_color(st.low, st)
    assert moisture_color(+5.0, st) == moisture_color(st.high, st)


# ------------------------------------------------------- когда фото нет

def _verdict(**kw):
    args = dict(area_ha=10.0, irrigation_method="furrow", has_polygon=True,
                scene_day=FRESH, today=TODAY, valid_fraction=1.0)
    args.update(kw)
    return can_show_photo(**args)


def test_photo_is_shown_when_everything_is_in_place():
    v = _verdict()
    assert v.ok and v.reason is None and v.scene_age_days == 3


def test_no_contour_no_photo():
    assert _verdict(has_polygon=False).reason == "no_contour"


def test_photo_ignores_the_irrigation_method():
    """Осознанное отступление от §4.6: «только борозда» относилось к
    заливке вдоль борозды, а мы показываем ИЗМЕРЕННУЮ влажность — замеру
    всё равно, как поливают. Для капли карта даже нужнее: забитый
    капельник не видно никак, а сухое пятно вокруг него читается."""
    assert _verdict(irrigation_method="drip").ok
    assert _verdict(irrigation_method="sprinkler").ok


def test_small_field_gets_no_photo():
    """Порог по клеткам ЗАМЕРА (B11, 20 м), а не по подложке 10 м:
    виноградник Фарруха в 1,5 га даёт настоящий перепад (размах NDMI
    0,21, не проседающий при отсечении кромки), и резать его порогом,
    выведенным для другой величины, значит прятать рабочую карту."""
    assert _verdict(area_ha=1.0).reason == "too_small"
    assert _verdict(area_ha=None).reason == "too_small"
    assert _verdict(area_ha=1.5).ok
    assert _verdict(area_ha=MIN_AREA_HA).ok


def test_stale_scene_is_refused_not_passed_off_as_today():
    """Больше двух недель без чистого кадра — честно сказать, что
    свежего снимка нет, а не показать старый как сегодняшний (§1.3)."""
    old = TODAY - timedelta(days=MAX_SCENE_AGE_DAYS + 1)
    v = _verdict(scene_day=old)
    assert v.reason == "scene_stale" and v.scene_age_days == 15

    edge = TODAY - timedelta(days=MAX_SCENE_AGE_DAYS)
    assert _verdict(scene_day=edge).ok
    assert _verdict(scene_day=None).reason == "no_scene"


def test_clouded_field_is_refused():
    assert _verdict(valid_fraction=0.5).reason == "clouded"
    assert _verdict(valid_fraction=0.8).ok


def test_rules_are_checked_in_order_of_what_the_farmer_can_fix():
    """У поля без контура и на двух гектарах причина — контур: это
    единственный шаг, который фермер может сделать прямо сейчас."""
    v = can_show_photo(area_ha=1.0, irrigation_method="drip",
                       has_polygon=False, scene_day=None, today=TODAY,
                       valid_fraction=0.0)
    assert v.reason == "no_contour"


# ---------------------------------------------------------------- кадр

RING = [[66.0, 39.0], [66.01, 39.0], [66.01, 39.01], [66.0, 39.01],
        [66.0, 39.0]]


def test_bbox_pads_around_the_field():
    """Поле не должно упираться в край кадра: фермер узнаёт участок по
    соседям и дороге вокруг него."""
    lon0, lat0, lon1, lat1 = bbox_of(RING, pad=0.15)
    assert lon0 < 66.0 and lon1 > 66.01
    assert lat0 < 39.0 and lat1 > 39.01
    assert (lon1 - lon0) == pytest.approx(0.01 * 1.3, rel=1e-6)


def test_ring_maps_into_the_frame_with_y_pointing_down():
    box = bbox_of(RING, pad=0.0)
    px = ring_to_pixels(RING, box, 100, 100)
    assert px[0] == pytest.approx((0.0, 100.0))     # юго-запад — низ слева
    assert px[2] == pytest.approx((100.0, 0.0))     # северо-восток — верх справа


def test_scale_bar_is_a_round_number_under_a_quarter_of_the_frame():
    box = bbox_of(RING, pad=0.0)      # ~865 м по ширине на этой широте
    assert scale_bar_m(box) in (100, 200)
