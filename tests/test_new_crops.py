"""
Три культуры фазы 1: абрикос, люцерна, ячмень — и многолетники в мастере.

По структуре посевов страны (память проекта: косточковые, люцерна,
ячмень — очередь покрытия) и под клинья гиганта. Значения — FAO-56
табл. 11/12/22, не полевые измерения: тесты держат форму кривых и
санитарные диапазоны сезона, а не «правильные» кубометры.
"""

from datetime import date

import pytest

import bot.main as B
from suv.climate import STATIONS, season
from suv.crop import CROPS, root_depth, season_start, stage_and_kc
from suv.schedule import Field, simulate
from suv.soil import SOILS, WaterBalanceState


# ------------------------------------------------------------- справочник

def test_nine_crops_with_sane_envelopes():
    assert len(CROPS) == 9
    for c in CROPS.values():
        assert 0.2 <= c.kc_ini < c.kc_mid <= 1.2, c.key
        assert 0.4 <= c.root_depth_m <= 2.0, c.key
        assert 0.2 <= c.depletion_fraction <= 0.7, c.key
        assert 140 <= sum(c.stages) <= 290, c.key


def test_new_crop_models_are_deliberate():
    assert CROPS["apricot"].perennial
    assert CROPS["apricot"].ndvi_kc_model == "cover"      # крона над междурядьем
    assert CROPS["alfalfa"].perennial
    assert CROPS["alfalfa"].ndvi_kc_model == "linear"     # травостой — Calera
    assert not CROPS["barley"].perennial
    assert CROPS["barley"].typical_sowing[0] == 10        # озимый


def test_apricot_behaves_like_an_established_orchard():
    a = CROPS["apricot"]
    assert root_depth(a, 5, years_since_planting=8.0) == a.root_depth_m
    assert season_start(a, date(2018, 3, 15), date(2026, 8, 1)) == date(2026, 3, 15)
    kcs = [stage_and_kc(a, d).kc for d in (0, 60, 120, 200)]
    assert kcs[0] == a.kc_ini and max(kcs) == a.kc_mid


# ------------------------------------------------------- сезонная санитария

def _season_total(crop_key, method, start, days, wt=0.0):
    st = STATIONS["samarkand"]
    f = Field("T", "T", 1.0, st.lat, st.lon, st.elevation_m,
              CROPS[crop_key], SOILS["loam"], start, method,
              water_table_depth_m=wt)
    plan = simulate(f, season(st, start, days), WaterBalanceState(10.0, 0.2),
                    start)
    return sum(p.gross_m3 for p in plan)


def test_winter_barley_mid_season_sits_in_spring():
    """Смысловой замок вместо выдуманной нормы: Kc_mid ячменя обязан
    накрывать апрель (сев 1 октября), а сезонная подача — не превышать
    верх правдоподобия. Ноль вегетационных поливов по многолетним
    нормам осадков — свойство модели (корни всю зиму растут в мокрую
    почву), и врать «должно быть N кубов» без эталона тест не будет."""
    b = CROPS["barley"]
    apr15 = (date(2027, 4, 15) - date(2026, 10, 1)).days
    assert stage_and_kc(b, apr15).kc == b.kc_mid
    total = _season_total("barley", "furrow", date(2026, 10, 1), 240)
    assert 0 <= total < 7000, f"{total:.0f} m3/ha за сезон ячменя"


def test_alfalfa_is_thirsty_but_not_absurd():
    total = _season_total("alfalfa", "furrow", date(2026, 3, 10), 210)
    # Санитария, не норма: подача по борозде (КПД 0,55) с усреднённым
    # по укосам Kc. Эталона нет — тест держит только правдоподобие.
    assert 4000 < total < 18000, f"{total:.0f} m3/ha за сезон люцерны"


def test_apricot_on_drip_is_comparable_to_apple():
    apricot = _season_total("apricot", "drip", date(2026, 3, 15), 210)
    apple = _season_total("apple", "drip", date(2026, 3, 20), 210)
    assert 2000 < apricot < 9000
    assert abs(apricot - apple) / apple < 0.6     # соседние культуры, не близнецы


# ----------------------------------------------------------------- мастер

def test_wizard_offers_every_engine_crop_with_unique_labels():
    assert set(B.CROP_ORDER) == set(CROPS)
    labels = [B._crop_label(k) for k in B.CROP_ORDER]
    assert len(set(labels)) == len(labels)


def test_perennial_ages_map_to_sane_planting_years():
    assert set(B.AGE_BY_ANSWER.values()) == {1, 2, 3, 5, 10, 15}
    today = date(2026, 9, 11)
    for label, years in B.AGE_BY_ANSWER.items():
        month, day = CROPS["apple"].typical_sowing
        planted = date(today.year - years, month, day)
        assert planted < today
        # Возраст переживает root_depth: 3+ лет — взрослые корни.
        zr = root_depth(CROPS["apple"], 30,
                        years_since_planting=(today - planted).days / 365.25)
        if years >= 3:
            assert zr == CROPS["apple"].root_depth_m
