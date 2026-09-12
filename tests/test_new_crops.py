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




def test_fresh_field_does_not_say_the_soil_is_still_wet():
    """Поле, заведённое сейчас, не отвечает «полив не требуется».

    Мастер сохраняет last_irrigation_date=None, и _rewind при неизвестной
    дате стартует с нуля — «поле только что полито». Прогон на нормалях
    Самарканда от 12.09.2026: у хлопка, винограда и яблони за 14 дней не
    выпадает ни одного полива, порог RAW (129-149 мм) добирается за
    30-35 дней. Это не влажная почва, а отсчёт, начатый сегодня, — а
    первым, что читал новый фермер, было «Bu hafta sug'orish shart emas.
    Tuproq hali yetarlicha nam».
    """
    from suv.schedule import recommend

    today = date(2026, 9, 12)
    series = season(STATIONS["samarkand"], today, 14)
    planted = {"cotton": date(2026, 4, 10), "grape": date(2015, 3, 15),
               "apple": date(2012, 3, 20)}
    for key, planting in planted.items():
        fld = Field(field_id="TG-1", name="Mening dalam", hectares=1.0,
                    lat=39.65, lon=66.96, elevation_m=705.0,
                    crop=CROPS[key], soil=SOILS["loam"],
                    planting_date=planting, irrigation_method="furrow")
        rec = recommend(fld, series, WaterBalanceState(0.0, 0.20), today)
        assert rec.action_day is None, key  # сам дефект, без него тест пуст

        uz = B._rec_message(rec, None, "uz", anchored=False)
        ru = B._rec_message(rec, None, "ru", anchored=False)
        assert "shart emas" not in uz, key
        assert "не требуется" not in ru and "достаточно влажная" not in ru, key
        # Отказ и выход из него: одна кнопка, которая ставит якорь.
        assert "ayta olmayman" in uz and B.BTN_BAJARDIM in uz, key
        assert "не отвечаю" in ru and B.BTN_BAJARDIM in ru, key

        # А с якорем это по-прежнему обычный совет — отказ не должен
        # заодно съесть нормальный ответ политому полю.
        assert "shart emas" in B._rec_message(rec, None, "uz", anchored=True)




# ------------------------------------------ озимые: месяц -> дата сева

def test_sowing_month_never_lands_in_the_future():
    """Мастер спрашивает «в каком месяце ПОСЕЯЛИ» — прошедшим временем.

    12.09.2026 ответ «Sentabr» давал 15.09.2026: сев на три дня вперёд,
    dap отрицательный, и карточка непосеянного поля уверенно показывала
    начальную стадию.
    """
    from suv.crop import sowing_from_month
    today = date(2026, 9, 12)
    for key, crop in CROPS.items():
        for month in range(1, 13):
            assert sowing_from_month(crop, month, today) <= today, (key, month)
    # Текущий месяц: сеяли раньше сегодняшнего числа, а не 15-го.
    assert sowing_from_month(CROPS["barley"], 9, today) == today


def test_sowing_month_takes_the_last_month_that_already_happened():
    from suv.crop import sowing_from_month
    # «Oktabr» в сентябре — октябрь ПРОШЛОГО года: будущего сева не бывает.
    assert sowing_from_month(CROPS["barley"], 10,
                             date(2026, 9, 12)) == date(2025, 10, 1)
    # Тот же ответ в январе — текущий озимый сезон, а не позапрошлый.
    assert sowing_from_month(CROPS["barley"], 10,
                             date(2027, 1, 12)) == date(2026, 10, 1)
    # Яровые не сдвинулись: апрельский хлопок остаётся апрельским.
    assert sowing_from_month(CROPS["cotton"], 4,
                             date(2026, 9, 12)) == date(2026, 4, 10)


def test_wizard_month_answer_goes_through_the_crop_calendar(monkeypatch):
    """Тот же замок на живом хендлере мастера: pytest-asyncio в пакете
    нет, хендлер крутится через asyncio.run (образец — test_multifield)."""
    import asyncio
    from types import SimpleNamespace

    class _Msg:
        def __init__(self, text):
            self.text = text

        async def reply_text(self, *a, **k):
            return None

    class _Upd:
        def __init__(self, text):
            self.message = _Msg(text)

    today = date(2026, 9, 12)
    monkeypatch.setattr(B, "today_tashkent", lambda: today)

    ctx = SimpleNamespace(user_data={"crop": "barley"}, bot=None)
    asyncio.run(B.got_planting(_Upd("Sentabr"), ctx))
    assert ctx.user_data["planting"] == today

    ctx = SimpleNamespace(user_data={"crop": "barley"}, bot=None)
    asyncio.run(B.got_planting(_Upd("Oktabr"), ctx))
    assert ctx.user_data["planting"] == date(2025, 10, 1)


# --------------------------------------------- кончившийся сезон: отказ

def test_finished_annual_season_is_refused_not_priced():
    """Ячмень, посеянный 01.10.2025: на 12.09.2026 dap 346 при сумме
    стадий 225. stage_and_kc уходил в else и держал kc_end навсегда, а
    бот печатал «на этой неделе полив не требуется» — уверенный расчёт
    по стерне. Считать там нечего, и сказать надо именно это.
    """
    from suv.messages import recommendation_text
    from suv.schedule import recommend
    today = date(2026, 9, 12)
    st = STATIONS["samarkand"]
    f = Field("T", "Arpa dalasi", 4.0, st.lat, st.lon, st.elevation_m,
              CROPS["barley"], SOILS["loam"], date(2025, 10, 1), "furrow")
    rec = recommend(f, season(st, today, 14), WaterBalanceState(45.0, 1.3),
                    today)
    assert rec.reason_key == "season_over"
    assert rec.action_day is None and rec.gross_m3 == 0.0
    assert rec.saved_m3 is None          # экономии на убранном поле нет
    for lang in ("uz", "ru"):
        text = recommendation_text(rec, lang)
        assert "01.10.2025" in text, text
        assert "shart emas" not in text and "не требуется" not in text, text


def test_perennial_winter_is_not_a_finished_season():
    """Обратная сторона того же замка: у сада сезон не кончается.

    dap многолетника уходит за сумму стадий каждую зиму (яблоня: 240
    дней от распускания — это середина ноября), и отказ «сезон кончился»
    там был бы враньём: после терима дереву нужен послеуборочный полив.
    """
    from suv.crop import season_is_over
    from suv.schedule import recommend
    today = date(2026, 12, 20)
    apple = CROPS["apple"]
    dap = (today - season_start(apple, date(2018, 3, 20), today)).days
    assert dap > sum(apple.stages)
    assert not season_is_over(apple, dap)

    st = STATIONS["samarkand"]
    f = Field("T", "Olmazor", 2.0, st.lat, st.lon, st.elevation_m, apple,
              SOILS["loam"], date(2018, 3, 20), "drip")
    rec = recommend(f, season(st, today, 14), WaterBalanceState(45.0, 1.5),
                    today)
    assert rec.reason_key != "season_over"
