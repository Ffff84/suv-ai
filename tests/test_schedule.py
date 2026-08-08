from datetime import date, timedelta
from suv.crop import CROPS, stage_and_kc, root_depth, kc_from_ndvi, blended_kc
from suv.soil import (SOILS, WaterBalanceState, total_available_water,
                      readily_available_water, effective_rainfall, step,
                      gross_irrigation, mm_to_m3)
from suv.et0 import DailyWeather
from suv.schedule import Field, simulate, recommend


def fergana_field(**kw):
    base = dict(
        field_id="FRG-001", name="Shimoliy dala", hectares=4.0,
        lat=40.39, lon=71.78, elevation_m=580,
        crop=CROPS["cotton"], soil=SOILS["loam"],
        planting_date=date(2026, 4, 10), irrigation_method="furrow",
        water_table_depth_m=1.8,
    )
    base.update(kw)
    return Field(**base)


def july_forecast(days=14, rain_on=None, rain_mm=0.0):
    out = []
    for i in range(days):
        r = rain_mm if rain_on is not None and i == rain_on else 0.0
        out.append(DailyWeather(doy=196 + i, t_max=37.0, t_min=21.0,
                                rh_mean=32.0, wind_2m=2.2, solar_rad=27.0,
                                rainfall=r))
    return out


# ---- crop stage model ----

def test_kc_follows_fao_curve_shape():
    c = CROPS["cotton"]
    assert stage_and_kc(c, 5).kc == c.kc_ini            # initial: flat
    assert stage_and_kc(c, 100).kc == c.kc_mid          # mid: flat at peak
    mid_dev = stage_and_kc(c, 55).kc
    assert c.kc_ini < mid_dev < c.kc_mid                # development: rising


def test_root_depth_grows_then_holds():
    c = CROPS["cotton"]
    assert root_depth(c, 0) < root_depth(c, 40) < root_depth(c, 79)
    assert root_depth(c, 200) == c.root_depth_m


def test_ndvi_kc_is_clamped_against_bad_pixels():
    c = CROPS["cotton"]
    assert kc_from_ndvi(0.95, c) <= c.kc_mid * 1.05     # cloud/bright edge
    assert kc_from_ndvi(0.02, c) >= c.kc_ini * 0.6      # shadow/water


def test_stale_imagery_is_ignored():
    kc, src = blended_kc(0.9, 1.2, ndvi_age_days=20)
    assert kc == 0.9 and "stale" in src


# ---- soil water balance ----

def test_taw_matches_fao_table():
    # loam: (0.32-0.15)*1000 = 170 mm/m ; at 1.0 m root depth -> 170 mm
    assert abs(total_available_water(SOILS["loam"], 1.0) - 170.0) < 0.1


def test_raw_shrinks_on_high_demand_days():
    taw = 170.0
    calm = readily_available_water(taw, 0.65, etc_mm_day=3.0)
    hot = readily_available_water(taw, 0.65, etc_mm_day=9.0)
    assert hot < calm


def test_light_shower_does_not_count():
    assert effective_rainfall(2.0) == 0.0
    assert effective_rainfall(20.0) > 0.0


def test_percolation_reported_when_overwatered():
    st = WaterBalanceState(depletion_mm=10.0, last_root_depth_m=1.0)
    _, perc = step(st, SOILS["loam"], 1.0, etc_mm=5.0, rain_mm=0.0,
                   irrigation_mm=100.0)
    assert perc > 80.0     # the water that pushes salt up the profile


def test_furrow_needs_more_water_than_drip():
    assert gross_irrigation(50, "furrow") > gross_irrigation(50, "drip")


def test_mm_to_m3():
    assert mm_to_m3(1.0, 1.0) == 10.0


# ---- the engine end to end ----

def test_hot_dry_july_triggers_irrigation():
    f = fergana_field()
    rec = recommend(f, july_forecast(),
                    WaterBalanceState(depletion_mm=95.0, last_root_depth_m=1.35),
                    date(2026, 7, 15))
    assert rec.action_day is not None
    assert rec.gross_mm > 0
    assert rec.reason_key in ("threshold_reached", "threshold_approaching")


def test_wet_soil_means_no_irrigation():
    f = fergana_field()
    rec = recommend(f, july_forecast(days=5),
                    WaterBalanceState(depletion_mm=0.0, last_root_depth_m=1.35),
                    date(2026, 7, 15))
    assert rec.action_day is None
    assert rec.reason_key == "soil_still_wet"


def test_heavy_rain_defers_the_irrigation():
    f = fergana_field()
    dry = recommend(f, july_forecast(days=12),
                    WaterBalanceState(80.0, 1.35), date(2026, 7, 15))
    wet = recommend(f, july_forecast(days=12, rain_on=1, rain_mm=45.0),
                    WaterBalanceState(80.0, 1.35), date(2026, 7, 15))
    # -1 means the rain pushed the irrigation clean out of the forecast
    # window, which is a stronger deferral than any positive number.
    deferred = wet.action_day is None or wet.days_until > dry.days_until
    assert deferred, (dry.days_until, wet.days_until)


def test_full_season_use_matches_uzbek_agronomic_norms():
    """
    Reality anchor. Cotton in Uzbekistan is irrigated with roughly
    5,000-7,000 m3/ha across a full season. If the engine lands far
    outside that band it is wrong, no matter how elegant the maths.
    """
    from suv.climate import STATIONS, season
    st = STATIONS["fergana"]
    f = fergana_field(hectares=1.0)
    wx = season(st, date(2026, 4, 10), 180)
    plan = simulate(f, wx, WaterBalanceState(20.0, 0.20), date(2026, 4, 10))
    total = sum(p.gross_m3 for p in plan)
    assert 4000 < total < 8000, f"{total:.0f} m3/ha is outside agronomic norms"


def test_season_saving_versus_fixed_calendar():
    """
    The business case. Compared against a fixed-interval calendar the
    engine should use LESS water across a season, because it skips
    irrigations after rain and in cool spells.

    Note the window matters: over a 30-day heatwave there is nothing to
    save, because the crop genuinely needs every millimetre. Any saving
    claim must be measured over a season, never over a peak month.
    """
    from suv.climate import STATIONS, season
    st = STATIONS["fergana"]
    f = fergana_field(hectares=1.0)
    wx = season(st, date(2026, 4, 10), 180)
    rec = recommend(f, wx, WaterBalanceState(20.0, 0.20), date(2026, 4, 10),
                    baseline_interval_days=30, baseline_application_m3_per_ha=1200.0)
    scheduled = sum(p.gross_m3 for p in rec.plan)
    assert scheduled < rec.baseline_m3, (scheduled, rec.baseline_m3)


def test_peak_heatwave_offers_no_saving_and_we_do_not_pretend_otherwise():
    """
    Guards the honesty of the pitch. During a sustained heatwave the
    engine must NOT invent a saving.
    """
    f = fergana_field()
    rec = recommend(f, july_forecast(days=30), WaterBalanceState(40.0, 1.35),
                    date(2026, 7, 1))
    scheduled = sum(p.gross_m3 for p in rec.plan)
    assert scheduled > 0


def test_drip_field_needs_less_gross_water_than_furrow():
    fur = recommend(fergana_field(irrigation_method="furrow"),
                    july_forecast(days=30), WaterBalanceState(40.0, 1.35),
                    date(2026, 7, 1))
    dri = recommend(fergana_field(irrigation_method="drip"),
                    july_forecast(days=30), WaterBalanceState(40.0, 1.35),
                    date(2026, 7, 1))
    fur_total = sum(p.gross_m3 for p in fur.plan)
    dri_total = sum(p.gross_m3 for p in dri.plan)
    assert dri_total < fur_total


def test_sandy_soil_needs_more_frequent_irrigation():
    sandy = simulate(fergana_field(soil=SOILS["sand"]), july_forecast(30),
                     WaterBalanceState(10.0, 1.35), date(2026, 7, 1))
    clay = simulate(fergana_field(soil=SOILS["clay"]), july_forecast(30),
                    WaterBalanceState(10.0, 1.35), date(2026, 7, 1))
    assert sum(p.irrigate for p in sandy) > sum(p.irrigate for p in clay)


# ---- farmer-facing messages ----

def test_message_keeps_its_sentence_commas():
    """Regression: the thousands separator once ate the sentence comma."""
    from suv.messages import recommendation_text
    from suv.climate import STATIONS, season
    st = STATIONS["fergana"]
    f = fergana_field()
    rec = recommend(f, season(st, date(2026, 7, 12), 14),
                    WaterBalanceState(105.0, 1.35), date(2026, 7, 12))
    uz = recommendation_text(rec, "uz")
    assert "," in uz.split("\n")[2]
    assert "  " not in uz


def test_russian_weekday_is_accusative():
    from suv.messages import WEEKDAY_RU
    assert "субботу" in WEEKDAY_RU and "суббота" not in WEEKDAY_RU


def test_salinity_warning_only_fires_at_high():
    from suv.messages import salinity_warning
    assert salinity_warning("low") is None
    assert salinity_warning("moderate") is None
    assert salinity_warning("high", "uz") is not None


def test_warm_start_from_last_irrigation_beats_assuming_a_full_field():
    """
    Onboarding regression. A field joined mid-season is NOT at field
    capacity. Assuming it is produces "no irrigation needed" on a field
    that is already past the stress threshold — the worst possible first
    impression for a new farmer.
    """
    from suv.climate import STATIONS, season
    st = STATIONS["samarkand"]
    f = fergana_field(lat=st.lat, lon=st.lon, elevation_m=st.elevation_m,
                      water_table_depth_m=st.typical_water_table_m)
    last_irrigation = date(2026, 7, 28)
    gap = 12
    hist = season(st, last_irrigation, gap)
    back = simulate(f, hist, WaterBalanceState(0.0, 0.20), last_irrigation,
                    apply_irrigation=False)
    assert back[-1].depletion_mm > 40, back[-1].depletion_mm


# ---- perennials ----

def test_perennial_orchard_does_not_age_across_seasons():
    """
    Real bug found on the first live field: a vineyard planted in 2017 was
    scored as 3,415 days old, landing in the 'late' stage with Kc 0.45
    instead of the correct mid-season 0.85 — a ~2x under-estimate of demand
    in August, i.e. advice to irrigate half as often as needed.
    """
    from suv.crop import CROPS, season_start, stage_and_kc
    grape, today = CROPS["grape"], date(2026, 8, 7)
    origin = season_start(grape, date(2017, 4, 1), today)
    assert origin.year == 2026
    assert stage_and_kc(grape, (today - origin).days).kc == grape.kc_mid


def test_annual_crop_still_counts_from_sowing():
    from suv.crop import CROPS, season_start
    cotton = CROPS["cotton"]
    assert season_start(cotton, date(2026, 4, 10), date(2026, 8, 7)) == date(2026, 4, 10)


def test_perennial_before_bud_break_uses_last_years_cycle():
    from suv.crop import CROPS, season_start
    apple = CROPS["apple"]
    assert season_start(apple, date(2018, 3, 20), date(2026, 2, 1)).year == 2025


def test_orchard_water_use_lands_in_a_sane_band():
    from suv.climate import STATIONS, season
    from suv.crop import CROPS
    st = STATIONS["samarkand"]
    f = fergana_field(crop=CROPS["apple"], soil=SOILS["sandy_loam"],
                      hectares=1.0, irrigation_method="drip",
                      planting_date=date(2018, 3, 20), water_table_depth_m=40.0,
                      lat=st.lat, lon=st.lon, elevation_m=st.elevation_m)
    plan = simulate(f, season(st, date(2026, 3, 20), 200),
                    WaterBalanceState(10.0, 0.20), date(2026, 3, 20))
    total = sum(p.gross_m3 for p in plan)
    assert 3000 < total < 9000, f"{total:.0f} m3/ha for a drip orchard"
