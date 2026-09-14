"""
Многоукосная люцерна: пила Kc от события укоса.

Правило честности фичи: пила ЗАРАБАТЫВАЕТСЯ отметкой фермера. Без
события — усреднённый Kc 0.95, как отгружено с фазы 1: фаза пилы
неизвестна, и усреднение честнее выдуманной точности. Тесты держат
обе половины: что событие включает цикл, и что его отсутствие
байт-в-байт сохраняет прежнее поведение.
"""

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import bot.main as B
from suv.climate import STATIONS, season
from suv.crop import (ALFALFA_CYCLE_CAP_DAYS, ALFALFA_KC_CUT,
                      ALFALFA_KC_PEAK, CROPS, cutting_cycle_kc, kc_from_ndvi)
from suv.field_status import water_section
from suv.ledger import Ledger
from suv.schedule import Field, recommend, simulate
from suv.soil import SOILS, WaterBalanceState

ST = STATIONS["samarkand"]
TODAY = date(2026, 6, 20)


def _beda(cut_dates=None, **kw):
    return Field("T-BEDA", "Beda", 1.0, ST.lat, ST.lon, ST.elevation_m,
                 CROPS["alfalfa"], SOILS["loam"], date(2020, 3, 10),
                 "furrow", cut_dates=list(cut_dates or []), **kw)


def _plan(fld, days=14, today=TODAY):
    return simulate(fld, season(ST, today, days),
                    WaterBalanceState(30.0, 1.5), today)


# ------------------------------------------------------------ чистая кривая

def test_regrowth_ramp_shape():
    assert cutting_cycle_kc(-1) is None            # укос из будущего — не цикл
    for d in range(5):
        assert cutting_cycle_kc(d) == ALFALFA_KC_CUT
    ramp = [cutting_cycle_kc(d) for d in range(5, 20)]
    assert ramp == sorted(ramp) and ramp[0] == ALFALFA_KC_CUT
    for d in range(20, ALFALFA_CYCLE_CAP_DAYS):
        assert cutting_cycle_kc(d) == ALFALFA_KC_PEAK
    assert cutting_cycle_kc(ALFALFA_CYCLE_CAP_DAYS) is None


# ------------------------------------------------------------------ движок

def test_no_cut_is_identical_to_shipped_curve():
    ref = _plan(_beda())
    empty = _plan(_beda(cut_dates=[]))
    for a, b in zip(ref, empty):
        assert (a.kc, a.kc_source, a.depletion_mm) == (
            b.kc, b.kc_source, b.depletion_mm)
    assert ref[0].kc == 0.95 and ref[0].kc_source == "calendar"


def test_cut_drops_kc_and_names_the_source():
    plan = _plan(_beda(cut_dates=[TODAY]))
    assert plan[0].kc == ALFALFA_KC_CUT
    assert plan[0].kc_source == "cut-cycle"


def test_second_cut_restarts_sawtooth():
    first = TODAY - timedelta(days=35)
    plan = _plan(_beda(cut_dates=[first, TODAY]), days=10)
    # день перед вторым укосом жил бы на плато первого цикла — но мы
    # стартуем с TODAY: день 0 — свежий срез второго цикла.
    assert plan[0].kc == ALFALFA_KC_CUT
    assert plan[9].kc > ALFALFA_KC_CUT               # отрастание пошло


def test_cycle_cap_reverts_to_averaged_mid_window():
    old_cut = TODAY - timedelta(days=48)
    plan = _plan(_beda(cut_dates=[old_cut]), days=6)
    assert plan[0].kc == ALFALFA_KC_PEAK             # день 48: цикл жив
    assert plan[2].kc == 0.95                        # день 50: истёк, усреднение
    assert plan[2].kc_source == "calendar"


def test_future_dated_cut_is_inert():
    plan = _plan(_beda(cut_dates=[TODAY + timedelta(days=5)]), days=8)
    assert plan[0].kc == 0.95                        # до укоса — как без него
    assert plan[5].kc == ALFALFA_KC_CUT              # с укоса — цикл


def test_scene_cut_partition_both_directions():
    # (а) Снимок ДО укоса на послеукосных днях: спутник молчит — иначе
    # высокий доукосный NDVI тянул бы отмеченные 0.40 обратно к 0.9.
    pre = _beda(cut_dates=[TODAY], ndvi=0.85,
                ndvi_date=TODAY - timedelta(days=2))
    plan = _plan(pre, days=5)
    assert plan[0].kc == ALFALFA_KC_CUT
    assert "satellite" not in plan[0].kc_source
    # (б) Снимок ПОСЛЕ укоса на ДОукосных днях истории: тоже молчит —
    # свежая послеукосная сцена весит как свежая (правило отрицательного
    # возраста blended_kc) и давила бы честное плато истории.
    cut = TODAY + timedelta(days=3)
    post = _beda(cut_dates=[cut], ndvi=0.30,
                 ndvi_date=cut + timedelta(days=1))
    plan = _plan(post, days=8)
    assert "satellite" not in plan[0].kc_source      # доукосная история чиста
    assert "satellite" in plan[5].kc_source          # одна сторона — снова вместе


def test_cycle_envelope_widened_for_regrowth():
    beda = CROPS["alfalfa"]
    # Усреднённый конверт резал бы измеренное отрастание к ~1.0.
    assert kc_from_ndvi(0.9, beda) == pytest.approx(0.9975)
    assert kc_from_ndvi(0.9, beda,
                        hi_override=ALFALFA_KC_PEAK * 1.05) == pytest.approx(1.196)


def test_root_depth_and_non_alfalfa_untouched():
    plan = _plan(_beda(cut_dates=[TODAY]))
    assert all(p.root_depth_m == 1.5 for p in plan)  # корни укосом не срезаются
    cotton = Field("T-C", "Paxta", 1.0, ST.lat, ST.lon, ST.elevation_m,
                   CROPS["cotton"], SOILS["loam"], date(2026, 4, 10),
                   "furrow", cut_dates=[TODAY])      # случайные данные
    plan_c = simulate(cotton, season(ST, TODAY, 5),
                      WaterBalanceState(30.0, 1.0), TODAY)
    assert plan_c[0].kc_source == "calendar"         # флаг культуры, не поле


def test_october_cut_is_not_a_finished_season():
    today = date(2026, 10, 20)
    fld = _beda(cut_dates=[date(2026, 10, 1)])
    rec = recommend(fld, season(ST, today, 14),
                    WaterBalanceState(30.0, 1.5), today)
    assert rec.reason_key != "season_over"


# ------------------------------------------------------------------ леджер

def test_log_cut_round_trip_and_retraction(tmp_path):
    L = Ledger(str(tmp_path / "cuts.db"))
    L.upsert_field(field_id="F1", name="B", hectares=1.0, lat=39.6,
                   lon=66.9, elevation_m=700.0, crop_key="alfalfa",
                   soil_key="loam", planting_date="2020-03-10",
                   irrigation_method="furrow")
    L.log_cut("F1", 111, date(2026, 6, 1))
    L.log_cut("F1", 111, date(2026, 6, 1))           # дубль того же дня
    L.log_cut("F1", 111, date(2026, 7, 10))
    assert L.cuts("F1", date(2026, 3, 10)) == [date(2026, 6, 1),
                                               date(2026, 7, 10)]
    assert L.cuts("F1", date(2026, 7, 1)) == [date(2026, 7, 10)]  # since
    # Ретракция гасит дату — но только строки, записанные ДО неё:
    L.log_cut("F1", 999, date(2026, 6, 1), source="retraction", note="мистап")
    assert L.cuts("F1", date(2026, 3, 10)) == [date(2026, 7, 10)]
    # поздний законный укос тем же числом (другой сезон) живёт.
    L.log_cut("F1", 111, date(2026, 6, 1))
    assert date(2026, 6, 1) in L.cuts("F1", date(2026, 3, 10))


# ------------------------------------------------------------------- гейт

def test_orim_gate_empty_is_closed_even_for_allowed(monkeypatch):
    """Семантика кабинета, не allowlist'а: пусто = закрыто ВСЕМ."""
    monkeypatch.setattr(B, "_ORIM", set())
    assert not B._orim_open(111)
    monkeypatch.setattr(B, "_ORIM", {42})
    assert B._orim_open(42) and not B._orim_open(111)


def test_orim_button_refuses_when_gate_closed(monkeypatch):
    """Залипшая кнопка у чата вне демо — внятный отказ, не тишина и не
    вопрос Амиру (BTN_ORIM в _handled)."""
    monkeypatch.setattr(B, "_ORIM", set())
    replies = []

    class _Msg:
        text = B.BTN_ORIM

        async def reply_text(self, text, **kw):
            replies.append(text)

    upd = SimpleNamespace(message=_Msg(),
                          effective_chat=SimpleNamespace(id=777))
    asyncio.run(B.orim(upd, SimpleNamespace(user_data={}, args=None)))
    assert replies and "yopiq sinov" in replies[0]


def test_menu_shows_orim_only_with_gate_and_alfalfa(monkeypatch):
    monkeypatch.setattr(B, "_ORIM", {42})
    monkeypatch.setattr(B, "_alfalfa_fields",
                        lambda chat: [{"field_id": "F1"}] if chat == 42 else [])
    def flat(chat):
        return [b for row in B._menu(chat).keyboard for b in row]
    assert any(B.BTN_ORIM == getattr(b, "text", b) for b in flat(42))
    assert not any(B.BTN_ORIM == getattr(b, "text", b) for b in flat(111))


# ----------------------------------------------------------------- карточка

def test_card_names_only_a_live_cut():
    fld = _beda(cut_dates=[TODAY - timedelta(days=5)])
    rec = recommend(fld, season(ST, TODAY, 14),
                    WaterBalanceState(30.0, 1.5), TODAY)
    for lang, marker in (("uz", "O'rim:"), ("ru", "Укос:")):
        sec = water_section(rec, TODAY - timedelta(days=3), TODAY, lang=lang)
        assert marker in sec.hint, lang
    stale = _beda(cut_dates=[TODAY - timedelta(days=60)])
    rec2 = recommend(stale, season(ST, TODAY, 14),
                     WaterBalanceState(30.0, 1.5), TODAY)
    sec2 = water_section(rec2, TODAY - timedelta(days=3), TODAY, lang="uz")
    assert "O'rim:" not in sec2.hint                 # истёкший цикл — не режим
