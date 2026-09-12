"""
Равномерность по многолетнему композиту: математика и честные отказы.

Зоны продуктивности OneSoil, повёрнутые под наш вопрос «доходит ли вода
до конца поля». Тесты держат: относительную шкалу (свой сезон — своё
среднее), стабильность классов, профиль вдоль хода воды, объявленные
пороги применимости и текст секции с тремя честными числами.
"""

import numpy as np
import pytest

from suv.field_status import (REACH_ALERT_PCT, REACH_OK_PCT, Status,
                              uniformity_section)
from suv.uniformity import (LOW_T, MIN_SEASONS, MIN_STABLE_SHARE, classify,
                            compose, reach_profile, relative_grid,
                            season_mean, stability)

H = W = 16
INSIDE = np.ones((H, W), dtype=bool)

# Кольцо-прямоугольник, вытянутый на восток (ход воды = 90°).
RING = [[66.996, 39.558], [66.9995, 39.558], [66.9995, 39.5593],
        [66.996, 39.5593], [66.996, 39.558]]


def _dry_tail_grid(level=0.30):
    """Поле, у которого восточный (дальний) край стабильно хуже."""
    g = np.full((H, W), 0.60, dtype="float32")
    g[:, -4:] = 0.60 * (1 - level)
    return g


# ------------------------------------------------------------- математика

def test_season_mean_uses_only_clear_frames():
    a = np.full((2, 2), 0.5, "float32")
    b = np.full((2, 2), 0.9, "float32")
    clear_a = np.array([[True, True], [True, False]])
    clear_b = np.array([[True, False], [False, False]])
    mean, known = season_mean([(a, clear_a), (b, clear_b)])
    assert mean[0, 0] == pytest.approx(0.7)     # оба кадра
    assert mean[0, 1] == pytest.approx(0.5)     # только первый
    assert not known[1, 1]                      # не видел ни разу


def test_relative_grid_is_per_season_scale():
    g = np.full((H, W), 0.4, "float32")
    g[:, :2] = 0.2
    rel = relative_grid(g, np.ones_like(g, bool), INSIDE)
    assert np.nanmean(rel) == pytest.approx(1.0, abs=0.01)
    assert rel[0, 0] < LOW_T                    # сухой край ниже порога


def test_stability_counts_consistent_pixels():
    lows = np.full((H, W), 0.95, "float32")
    lows[:, -4:] = 0.5
    rels = [relative_grid(lows, np.ones_like(lows, bool), INSIDE)
            for _ in range(4)]
    share, stable_low = stability([classify(r) for r in rels], INSIDE)
    assert share == pytest.approx(1.0)
    assert stable_low[:, -1].all() and not stable_low[:, 0].any()


def test_flipping_zones_are_not_stable():
    a = np.full((H, W), 0.6, "float32"); a[:, :8] = 0.4
    b = np.full((H, W), 0.6, "float32"); b[:, 8:] = 0.4
    rels = [relative_grid(g, np.ones_like(g, bool), INSIDE)
            for g in (a, b, a, b)]
    share, _ = stability([classify(r) for r in rels], INSIDE)
    assert share < 0.2                          # мигающие зоны — не почерк


def test_reach_profile_sees_the_dry_far_end():
    rel = relative_grid(_dry_tail_grid(), np.ones((H, W), bool), INSIDE)
    means, tail = reach_profile(rel, INSIDE, RING, flow_bearing_deg=90.0)
    assert means[0] > means[-1]
    assert tail is not None and tail < REACH_ALERT_PCT
    # Тот же контур, ход воды в обратную сторону: «дальний» край меняется.
    _, tail_back = reach_profile(rel, INSIDE, RING, flow_bearing_deg=270.0)
    assert tail_back > 0


def test_compose_collects_three_honest_numbers():
    rels = [relative_grid(_dry_tail_grid(), np.ones((H, W), bool), INSIDE)
            for _ in range(4)]
    got = compose(rels + [None], INSIDE)        # пустой сезон не считается
    assert got["seasons_used"] == 4
    assert 0.0 <= got["stable_share"] <= 1.0
    assert got["rel_mean"].shape == (H, W)
    assert compose([None, None], INSIDE) is None


def test_applicability_thresholds_are_the_published_ones():
    assert MIN_SEASONS == 3 and MIN_STABLE_SHARE == 0.40


# ---------------------------------------------------------------- секция

def _reach(tail, refused=None, n=5, share=0.62):
    return {"seasons_used": n, "stable_share": share, "refused": refused,
            "tail_pct": tail, "built": "2026-09-11"}


def test_section_ok_warn_alert_by_tail():
    ok = uniformity_section("furrow", 9.4, inlet_side="shimol",
                            reach=_reach(-3.0))
    warn = uniformity_section("furrow", 9.4, inlet_side="shimol",
                              reach=_reach(-11.0))
    alert = uniformity_section("furrow", 9.4, inlet_side="shimol",
                               reach=_reach(-22.0))
    assert ok.status is Status.OK and "oxirigacha yetadi" in ok.line
    assert warn.status is Status.WARN
    assert alert.status is Status.ALERT
    # Три честных числа в строке: сезоны, стабильность, край.
    assert "5 mavsum" in ok.line and "62%" in ok.line and "-3%" in ok.line
    assert REACH_OK_PCT == -7.0


def test_section_refusal_is_spoken_aloud():
    few = uniformity_section("furrow", 9.4, inlet_side="shimol",
                             reach=_reach(None, refused="few_seasons", n=2))
    unst = uniformity_section("furrow", 9.4, inlet_side="shimol",
                              reach=_reach(None, refused="unstable",
                                           share=0.25))
    assert few.status is Status.NO_DATA and "2 mavsum" in few.hint
    assert "taxmin qilmayman" in few.hint       # порог применимости вслух
    assert "25%" in unst.hint and "qurilmaydi" in unst.hint


def test_section_without_measurement_stays_honest():
    s = uniformity_section("furrow", 9.4, inlet_side="shimol", reach=None)
    assert s.status is Status.NO_DATA
    assert "o'lchov hali" in s.hint


def test_too_small_does_not_blame_the_number_of_frames():
    """Отказ обязан называть свою настоящую причину.

    Повод: 12.09.2026 ветка `else` печатала «Снимков мало (5 сезона)»
    для ЛЮБОГО отказа, кроме `unstable`. При `too_small` порог сезонов
    как раз пройден, а не построился профиль по восьми ломтикам вдоль
    борозды — фермеру сообщали неверную причину ровно на том экране,
    которым мы хвалимся как честным отказом.
    """
    small = uniformity_section("furrow", 0.4, inlet_side="shimol",
                               reach=_reach(None, refused="too_small", n=6))
    assert small.status is Status.NO_DATA
    assert "mavsum" not in small.hint, "причина подменена числом сезонов"
    assert "kichik" in small.hint          # «поле мало для профиля»

    ru = uniformity_section("furrow", 0.4, lang="ru", inlet_side="север",
                            reach=_reach(None, refused="too_small", n=6))
    assert "Снимков мало" not in ru.hint
    assert "мало для профиля" in ru.hint


@pytest.mark.parametrize("n,expect", [
    (1, "1 сезон"), (2, "2 сезона"), (4, "4 сезона"), (5, "5 сезонов"),
    (11, "11 сезонов"), (21, "21 сезон"), (22, "22 сезона"),
])
def test_russian_seasons_agree_with_the_number(n, expect):
    """«5 сезона» читает фермер, а не разработчик. Плоское «сезона»
    врало на всех числах, кроме 2-4."""
    sec = uniformity_section("furrow", 9.4, lang="ru", inlet_side="север",
                             reach=_reach(None, refused="few_seasons", n=n))
    assert expect in sec.hint




def test_section_names_where_the_flow_axis_came_from():
    """Замер по отметке фермера и замер по догадке «длинная ось» до сих
    пор выглядели в карточке одинаково. Разница не косметическая: во
    втором случае дальний край выбрали мы, а не вода."""
    mark = uniformity_section("furrow", 9.4, lang="ru", inlet_side="север",
                              reach=dict(_reach(-18.0), flow_source="inlet"))
    guess = uniformity_section("furrow", 9.4, lang="ru", inlet_side="север",
                               reach=dict(_reach(-18.0),
                                          flow_source="long_axis"))
    assert "по вашей отметке" in mark.hint
    assert "догадка по длинной оси" in guess.hint
    assert mark.hint != guess.hint

    uz = uniformity_section("furrow", 9.4, inlet_side="shimol",
                            reach=dict(_reach(-18.0),
                                       flow_source="long_axis"))
    assert "taxmin" in uz.hint

    # Замер старого формата: источник оси не записан — и не выдумывается.
    old = uniformity_section("furrow", 9.4, lang="ru", inlet_side="север",
                             reach=_reach(-18.0))
    assert "ход воды" not in old.hint


def test_measurement_along_another_axis_gives_no_verdict():
    """Сторону входа можно переназначить, не трогая контур. Тогда
    «дальний край» замера — про другой конец поля, и знак tail_pct
    зависит от того, какой конец считать ближним. Молчим."""
    stale = uniformity_section("furrow", 9.4, lang="ru", inlet_side="север",
                               reach=dict(_reach(-22.0), flow_source="inlet",
                                          stale_axis=True))
    assert stale.status is Status.NO_DATA, "вердикт по чужой оси"
    assert "%" not in stale.line
    assert "по другой оси" in stale.hint

    uz = uniformity_section("furrow", 9.4, inlet_side="shimol",
                            reach=dict(_reach(-22.0), flow_source="inlet",
                                       stale_axis=True))
    assert uz.status is Status.NO_DATA and "yo'nalishi" in uz.hint


def test_payload_marks_a_measurement_built_along_another_axis():
    """Сверка оси живёт в боте: геометрия поля есть только там."""
    import json

    import bot.main as B
    from suv.clock import today as today_tashkent
    from suv.trial import flow_bearing_from_inlet

    now = flow_bearing_from_inlet(RING, 0, 1)

    def _row(bearing):
        return {"field_id": "F-1", "polygon_geojson": json.dumps(RING),
                "inlet_vertices": json.dumps([0, 1]),
                "uniformity_json": json.dumps(
                    {"built": today_tashkent().isoformat(),
                     "flow_bearing_deg": bearing, "flow_source": "inlet",
                     "seasons_used": 5, "stable_share": 0.61,
                     "refused": None, "tail_pct": -22.0})}

    assert B._uniformity_payload(_row(now))["stale_axis"] is False
    assert B._uniformity_payload(_row((now + 90) % 360))["stale_axis"] is True
    # Ровно на пороге ось ещё та же: 15° — половина сектора розы.
    assert B._uniformity_payload(_row((now + 10) % 360))["stale_axis"] is False
