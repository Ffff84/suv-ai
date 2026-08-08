"""
Economics, anchored on the first real measurement we ever got from a farm:
one hour of pumping = 30 kWh = 60 m3 of water = 45,000 UZS, lift 40 m.
"""
import pytest

from suv.economics import (PumpProfile, cost_of, describe, pump_upgrade_saving,
                           savings)

FARM = PumpProfile(kwh_per_hour=30.0, m3_per_hour=60.0,
                   cost_per_hour_uzs=45_000.0, lift_m=40.0)


def test_derived_unit_costs():
    assert FARM.kwh_per_m3 == pytest.approx(0.5)
    assert FARM.uzs_per_m3 == pytest.approx(750.0)
    assert FARM.uzs_per_kwh == pytest.approx(1500.0)


def test_physics_floor_is_respected():
    """Lifting 1 m3 by 40 m cannot cost less than rho*g*h."""
    assert FARM.theoretical_kwh_per_m3 == pytest.approx(0.109, abs=0.002)
    assert FARM.kwh_per_m3 > FARM.theoretical_kwh_per_m3


def test_efficiency_flags_a_worn_pump():
    eff = FARM.efficiency
    assert eff is not None
    assert 0.15 < eff < 0.30, eff        # ~22% — far below a healthy 50-70%


def test_efficiency_unknown_without_lift():
    assert PumpProfile(30, 60, 45_000).efficiency is None


def test_cost_of_a_real_june():
    """8 sessions x 24 h x 24 m3/h on the apple orchard."""
    c = cost_of(8 * 24 * 24.0, FARM)
    assert c.m3 == pytest.approx(4608)
    assert c.kwh == pytest.approx(2304)
    assert c.uzs == pytest.approx(3_456_000)


def test_savings_can_be_negative_when_underwatering():
    """Under-irrigation is a real finding, not an error to be hidden."""
    s = savings(actual_m3=500, recommended_m3=900, pump=FARM)
    assert s.m3 < 0 and s.uzs < 0


def test_pump_upgrade_is_reported_separately_from_scheduling():
    up = pump_upgrade_saving(annual_m3=20_000, pump=FARM)
    assert up is not None and up.kwh > 0
    # a healthy pump would use far less per m3, so the gap is large
    assert up.uzs > 5_000_000


def test_no_upgrade_claim_for_an_already_good_pump():
    # 0.15 kWh/m3 at a 40 m lift is ~73% efficient — better than the 60%
    # target, so there is nothing to claim. The old fixture sat exactly on
    # the boundary and produced a meaningless 0.002 kWh "saving": a good
    # reminder that a threshold test needs to be clearly on one side.
    good = PumpProfile(kwh_per_hour=9.0, m3_per_hour=60.0,
                       cost_per_hour_uzs=13_500.0, lift_m=40.0)
    assert pump_upgrade_saving(20_000, good) is None


def test_describe_is_human_readable():
    d = describe(FARM)
    assert "кВт·ч/м³" in d and "КПД" in d


# ---- gravity-fed fields ----

def test_gravity_field_costs_no_electricity():
    """
    Farrukh's vineyard is canal-fed. Charging it electricity would invent
    a saving that does not exist for him.
    """
    from suv.economics import cost_of
    c = cost_of(5000.0, None)
    assert c.m3 == 5000 and c.kwh == 0 and c.uzs == 0


def test_gravity_saving_is_real_water_but_zero_money():
    s = savings(actual_m3=8000, recommended_m3=6000, pump=None)
    assert s.m3 == 2000
    assert s.uzs == 0


def test_value_story_separates_the_two_customers():
    from suv.economics import value_story
    assert "государству" in value_story(None)
    assert "деньги фермера" in value_story(FARM)


def test_describe_handles_gravity():
    assert "самотёк" in describe(None)


# ---- the farmer's unit is hours, not millimetres ----

def _rec_for_message():
    from datetime import date

    from suv.climate import STATIONS, season
    from suv.crop import CROPS
    from suv.schedule import Field, recommend
    from suv.soil import SOILS, WaterBalanceState
    st = STATIONS["samarkand"]
    f = Field("A", "Olmazor", 2.0, 39.558108, 66.996099, 700, CROPS["apple"],
              SOILS["sandy_loam"], date(2018, 3, 20), "drip",
              water_table_depth_m=40.0)
    return recommend(f, season(st, date(2026, 7, 20), 14),
                     WaterBalanceState(80.0, 1.5), date(2026, 7, 20))


def test_pumped_field_is_told_in_hours_and_money():
    """
    The first pilot farmer could not answer 'how many m3' but knew exactly
    how long he runs the pump. Hours are the control he operates.
    """
    from suv.messages import recommendation_text
    rec = _rec_for_message()
    ru = recommendation_text(rec, "ru", pump=FARM)
    assert "насос" in ru and "ч." in ru
    assert "сум" in ru
    uz = recommendation_text(rec, "uz", pump=FARM)
    assert "nasosni" in uz and "soat" in uz


def test_gravity_field_falls_back_to_millimetres():
    """No pump means no hours and — critically — no money claim."""
    from suv.messages import recommendation_text
    rec = _rec_for_message()
    ru = recommendation_text(rec, "ru", pump=None)
    assert "мм" in ru
    assert "сум" not in ru
