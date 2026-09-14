"""
Новые культуры. Фаза 1 (11.09.2026): абрикос, люцерна, ячмень — и
многолетники в мастере. Фаза 2 (14.09.2026): фасоль, маш, кукуруза,
картофель, дыня, арбуз, огурец, морковь, персик, черешня, гранат.

По структуре посевов страны и под клинья гиганта. Значения — FAO-56
табл. 11/12/22 (гранат — литература, в FAO-56 его нет), не полевые
измерения: тесты держат форму кривых и санитарные диапазоны сезона,
а не «правильные» кубометры.
"""

from datetime import date

import pytest

import bot.main as B
from suv.climate import STATIONS, season
from suv.crop import CROPS, root_depth, season_start, stage_and_kc
from suv.schedule import Field, simulate
from suv.soil import SOILS, WaterBalanceState


# ------------------------------------------------------------- справочник

def test_all_crops_have_sane_envelopes():
    assert len(CROPS) == 29
    for c in CROPS.values():
        assert 0.2 <= c.kc_ini < c.kc_mid <= 1.2, c.key
        assert 0.4 <= c.root_depth_m <= 2.0, c.key
        assert 0.2 <= c.depletion_fraction <= 0.7, c.key
        # Нижняя граница 85, не 140: короткосезонные бобовые и бахча по
        # FAO-56 законно живут 90-130 дней (маш 90 — самый короткий).
        assert 85 <= sum(c.stages) <= 290, c.key


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


# --------------------------------------- волна фазы 2: модели и санитария

def test_phase2_crop_models_are_deliberate():
    for key in ("peach", "cherry", "pomegranate"):
        assert CROPS[key].perennial, key
        assert CROPS[key].ndvi_kc_model == "cover", key      # крона над междурядьем
        assert CROPS[key].canopy_height_m > 0, key
    for key in ("beans", "mung", "maize", "potato", "melon", "watermelon",
                "cucumber", "carrot"):
        assert not CROPS[key].perennial, key
        assert CROPS[key].ndvi_kc_model == "linear", key     # травяной полог — Calera
    # Повторные культуры сеются летом — это их основной клин, не весна.
    assert CROPS["mung"].typical_sowing[0] == 7
    assert CROPS["beans"].typical_sowing[0] == 6
    assert CROPS["carrot"].typical_sowing[0] == 6
    # Гранат: сухая пауза КОРОЧЕ яблочной — кожуру рвёт дождь по
    # водно-стрессовому дереву (Galindo 2014), а не «налитый» плод.
    assert CROPS["pomegranate"].preharvest_hold_days < CROPS["apple"].preharvest_hold_days


def test_summer_mung_finishes_before_frost():
    """Июльский повторный сев доживает до конца сентября, не до ноября:
    строка табл. 11 сжата до 90 дней ровно ради этого."""
    from datetime import timedelta
    m = CROPS["mung"]
    end = date(2026, 7, 1) + timedelta(days=sum(m.stages))
    assert end.month == 9
    total = _season_total("mung", "furrow", date(2026, 7, 1), sum(m.stages))
    assert 2500 < total < 9000, f"{total:.0f} m3/ha за сезон маша"


def test_maize_is_the_thirstiest_annual_but_not_absurd():
    # Санитария, не норма: борозда (КПД 0,55), без грунтовых вод.
    total = _season_total("maize", "furrow", date(2026, 4, 20), 150)
    assert 5000 < total < 15000, f"{total:.0f} m3/ha за сезон кукурузы"


def test_peach_twins_apricot_on_drip():
    """Одна строка табл. 12 — сезоны обязаны почти совпадать; расходятся
    они только датой распускания."""
    peach = _season_total("peach", "drip", date(2026, 3, 20), 210)
    apricot = _season_total("apricot", "drip", date(2026, 3, 15), 210)
    assert abs(peach - apricot) / apricot < 0.15


def test_pomegranate_is_thriftier_than_apple():
    """kc_mid 0.85 — самый низкий среди садов, и сезон обязан быть
    скромнее яблоневого; если гранат вдруг обогнал яблоню — в записи
    ошибка, а не «особенность»."""
    pom = _season_total("pomegranate", "drip", date(2026, 4, 10), 210)
    apple = _season_total("apple", "drip", date(2026, 3, 20), 210)
    assert pom < apple
    assert 3000 < pom < 12000, f"{pom:.0f} m3/ha за сезон граната"


# ------------------------- волна 3: повторные циклы, соя, капуста, сад

def test_wave3_crop_models_are_deliberate():
    from suv.crop import INTERNAL_CROPS
    for key in ("plum", "persimmon"):
        assert CROPS[key].perennial and CROPS[key].ndvi_kc_model == "cover", key
    for key in ("soybean", "cabbage", "maize_second", "potato_summer"):
        assert not CROPS[key].perennial, key
        assert CROPS[key].ndvi_kc_model == "linear", key
    # Повторные циклы — июльский сев, и оба ключа внутренние.
    assert CROPS["maize_second"].typical_sowing[0] == 7
    assert CROPS["potato_summer"].typical_sowing[0] == 7
    assert INTERNAL_CROPS == {"maize_second", "potato_summer"}
    # Капуста: дата — ВЫСАДКА рассады, кривая стартует с поля.
    assert CROPS["cabbage"].typical_sowing == (3, 10)
    # Летний картофель кончается сноской «убитая ботва», не зелёной копкой.
    assert CROPS["potato_summer"].kc_end < CROPS["potato"].kc_end


def test_month_resolves_the_second_cycle_not_a_button():
    """«Makkajo'xori, iyul» и «Makkajo'xori, aprel» — разные кривые под
    одной кнопкой: развилка живёт в resolve_cycle, а не в клавиатуре."""
    from suv.crop import resolve_cycle
    assert resolve_cycle("maize", 7) == "maize_second"
    assert resolve_cycle("maize", 6) == "maize_second"
    assert resolve_cycle("maize", 4) == "maize"
    assert resolve_cycle("potato", 7) == "potato_summer"
    assert resolve_cycle("potato", 3) == "potato"
    # Культуры без двойника проходят как есть — любым месяцем.
    for month in range(1, 13):
        assert resolve_cycle("cotton", month) == "cotton"
        assert resolve_cycle("maize_second", month) == "maize_second"


def test_wizard_month_answer_switches_the_cycle(monkeypatch):
    """Живой хендлер мастера: июль превращает кукурузу в повторную,
    апрель оставляет весеннюю (образец — тест озимых выше)."""
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

    ctx = SimpleNamespace(user_data={"crop": "maize"}, bot=None)
    asyncio.run(B.got_planting(_Upd("Iyul"), ctx))
    assert ctx.user_data["crop"] == "maize_second"
    assert ctx.user_data["planting"] == date(2026, 7, 1)

    ctx = SimpleNamespace(user_data={"crop": "maize"}, bot=None)
    asyncio.run(B.got_planting(_Upd("Aprel"), ctx))
    assert ctx.user_data["crop"] == "maize"


def test_second_cycles_fit_before_frost_and_cost_less():
    """110/115 дней от 1 июля кончаются в октябре, до заморозка, и
    повторная кукуруза обязана стоить заметно дешевле весенней —
    полсезона против полного."""
    from datetime import timedelta
    for key in ("maize_second", "potato_summer"):
        end = date(2026, 7, 1) + timedelta(days=sum(CROPS[key].stages))
        assert end.month == 10, key
    second = _season_total("maize_second", "furrow", date(2026, 7, 1), 110)
    spring = _season_total("maize", "furrow", date(2026, 4, 20), 150)
    assert second < spring * 0.75
    assert 2500 < second < 9000, f"{second:.0f} m3/ha повторной кукурузы"


def test_soybean_and_cabbage_seasons_are_plausible():
    soy = _season_total("soybean", "furrow", date(2026, 5, 1), 135)
    assert 6000 < soy < 16000, f"{soy:.0f} m3/ha за сезон сои"
    cab = _season_total("cabbage", "furrow", date(2026, 3, 10), 95)
    assert 2000 < cab < 8000, f"{cab:.0f} m3/ha за сезон капусты"


def test_plum_twins_apricot_and_persimmon_is_thrifty():
    plum = _season_total("plum", "drip", date(2026, 3, 20), 210)
    apricot = _season_total("apricot", "drip", date(2026, 3, 15), 210)
    assert abs(plum - apricot) / apricot < 0.15  # одна строка табл. 12
    pers = _season_total("persimmon", "drip", date(2026, 4, 10), 220)
    apple = _season_total("apple", "drip", date(2026, 3, 20), 210)
    assert pers < apple  # kc_mid 0.85 обязан быть скромнее яблони


# --------------------- волна 4: рис (гейт затопления), кунжут, свёкла

def test_wave4_crop_models_are_deliberate():
    from suv.crop import PADDY_CROPS
    for key in ("rice", "sesame", "sugar_beet"):
        assert not CROPS[key].perennial, key
        assert CROPS[key].ndvi_kc_model == "linear", key
    assert PADDY_CROPS == {"rice"}
    # Кунжут — повторный, но сеять позже конца июня уже поздно.
    assert CROPS["sesame"].typical_sowing == (6, 20)
    # Свёкла — справочник без кнопки, но НЕ внутренний ключ цикла.
    from suv.crop import INTERNAL_CROPS
    assert "sugar_beet" in B.CATALOG_ONLY_CROPS
    assert "sugar_beet" not in INTERNAL_CROPS
    # Рис не терпит стресса: p на нижней санитарной границе сознательно.
    assert CROPS["rice"].depletion_fraction == 0.20


def test_flooded_rice_is_refused_not_computed():
    """Рис по чекам — честный отказ, а не расчёт: под слоем воды неверно
    каждое число, поэтому и план пуст (в отличие от season_over)."""
    from suv.messages import recommendation_text, why_text
    from suv.schedule import recommend
    today = date(2026, 7, 10)
    st = STATIONS["samarkand"]
    series = season(st, today, 14)
    # ALLOW-list: обе борозды — чек, включая лазерную планировку.
    for method in ("furrow", "furrow_improved"):
        f = Field("T", "Sholi dala", 2.0, st.lat, st.lon, st.elevation_m,
                  CROPS["rice"], SOILS["loam"], date(2026, 5, 10), method)
        rec = recommend(f, series, WaterBalanceState(10.0, 0.6), today)
        assert rec.reason_key == "rice_flooded", method
        assert rec.action_day is None and rec.gross_m3 == 0.0
        assert rec.plan == [] and rec.saved_m3 is None
        for lang in ("uz", "ru"):
            text = recommendation_text(rec, lang)
            assert "shart emas" not in text and "не требуется" not in text
        assert "bostirib" in why_text(rec, None, "uz")
    # Безводный рис проходит гейт в обычный расчёт.
    for method in ("drip", "sprinkler"):
        f = Field("T", "Sholi dala", 2.0, st.lat, st.lon, st.elevation_m,
                  CROPS["rice"], SOILS["loam"], date(2026, 5, 10), method)
        rec = recommend(f, series, WaterBalanceState(10.0, 0.6), today)
        assert rec.reason_key != "rice_flooded", method
        assert rec.plan


def test_flooded_rice_refusal_survives_the_missing_anchor():
    """Свежее поле мастера (якоря нет): отказ по методу полива главнее
    отказа «не знаю, когда поливали» — иначе рисовода звали бы отметить
    полив на поле, которое бот считать не будет никогда."""
    from suv.schedule import recommend
    today = date(2026, 7, 10)
    st = STATIONS["samarkand"]
    f = Field("T", "Sholi dala", 2.0, st.lat, st.lon, st.elevation_m,
              CROPS["rice"], SOILS["loam"], date(2026, 5, 10), "furrow")
    rec = recommend(f, season(st, today, 14), WaterBalanceState(0.0, 0.6),
                    today)
    for lang, marker in (("uz", "bostirib"), ("ru", "затоплением")):
        msg = B._rec_message(rec, None, lang, anchored=False)
        assert marker in msg, msg
        assert "ayta olmayman" not in msg and "не отвечаю" not in msg
        assert B.BTN_BAJARDIM not in msg


def test_upland_rice_and_minor_annuals_seasons_are_plausible():
    # Капельный рис ~9 000 м³/га против ~16 700 затопленной нормы — в
    # этом и есть водосберегающая история; полосы — санитария, не норма.
    rice = _season_total("rice", "drip", date(2026, 5, 10), 135)
    assert 5000 < rice < 14000, f"{rice:.0f} m3/ha капельного риса"
    ses = _season_total("sesame", "furrow", date(2026, 6, 20), 110)
    assert 2500 < ses < 9000, f"{ses:.0f} m3/ha за сезон кунжута"
    beet = _season_total("sugar_beet", "furrow", date(2026, 4, 5), 180)
    assert 7000 < beet < 20000, f"{beet:.0f} m3/ha за сезон свёклы"


# ----------------------------------------------------------------- мастер

def test_wizard_offers_every_engine_crop_with_unique_labels():
    """Каждая культура движка — кнопка, КРОМЕ внутренних ключей
    повторных циклов (их выбирает месяц сева, не фермер) и справочных
    записей без живого спроса (CATALOG_ONLY_CROPS)."""
    from suv.crop import INTERNAL_CROPS
    assert set(B.CROP_ORDER) == set(CROPS) - INTERNAL_CROPS - B.CATALOG_ONLY_CROPS
    assert not (INTERNAL_CROPS | B.CATALOG_ONLY_CROPS) & set(B.CROP_ORDER)
    labels = [B._crop_label(k) for k in B.CROP_ORDER]
    assert len(set(labels)) == len(labels)
    # У внутренних ключей есть эмодзи и имя: карточка поля их печатает.
    for key in INTERNAL_CROPS:
        assert key in B.CROP_EMOJI and CROPS[key].name_uz


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
