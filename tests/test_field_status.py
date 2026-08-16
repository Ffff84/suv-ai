"""
Экран «Dala holati»: реестр секций и вёрстка карточки.

Закреплённые тестами правила из ТЗ:
  - общий статус = максимум по секциям, NO_DATA в расчёт не идёт;
  - секция None (неприменима) не рисуется совсем;
  - статус всегда словом, никогда только цветом;
  - самотёку деньги не показываются.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date, timedelta

from suv.economics import PumpProfile
from suv.et0 import DailyWeather
from suv.field_status import (Section, Status, assemble, cost_section,
                              field_list_label, overall_status, render_card,
                              uniformity_section, water_section,
                              weather_section)


def _section(status: Status, key: str = "x", order: int = 10) -> Section:
    return Section(key=key, order=order, title="t", status=status, line="l")


# ---------------------------------------------------------------- реестр

def test_overall_is_max_and_no_data_does_not_count():
    assert overall_status([_section(Status.OK),
                           _section(Status.WARN)]) is Status.WARN
    assert overall_status([_section(Status.ALERT),
                           _section(Status.OK)]) is Status.ALERT
    # Незаполненный слот не делает поле красным — и не делает зелёным.
    assert overall_status([_section(Status.NO_DATA),
                           _section(Status.OK)]) is Status.OK
    assert overall_status([_section(Status.NO_DATA)]) is Status.NO_DATA
    assert overall_status([]) is Status.NO_DATA


def test_assemble_drops_none_and_sorts_by_order():
    weather = _section(Status.OK, "weather", order=30)
    water = _section(Status.OK, "water", order=10)
    out = assemble(weather, None, water)
    assert [s.key for s in out] == ["water", "weather"]


# ---------------------------------------------------------------- вода

@dataclass
class _Plan:
    stress: bool = False


@dataclass
class _Rec:
    action_day: date | None = None
    days_until: int = -1
    gross_m3: float = 500.0
    plan: list = dc_field(default_factory=list)


TODAY = date(2026, 8, 15)
PUMP = PumpProfile(kwh_per_hour=30.0, m3_per_hour=100.0,
                   cost_per_hour_uzs=20_000.0)


def test_water_no_action_is_ok():
    s = water_section(_Rec(), TODAY - timedelta(days=3), TODAY)
    assert s.status is Status.OK
    assert "shart emas" in s.line
    assert "3 kun oldin" in s.hint


def test_water_today_is_warn_and_stress_is_alert():
    rec = _Rec(action_day=TODAY, days_until=0, plan=[_Plan(stress=False)])
    assert water_section(rec, TODAY - timedelta(days=9),
                         TODAY).status is Status.WARN

    rec = _Rec(action_day=TODAY, days_until=0, plan=[_Plan(stress=True)])
    s = water_section(rec, TODAY - timedelta(days=9), TODAY)
    assert s.status is Status.ALERT
    assert "kechikmoqda" in s.line


def test_water_today_with_pump_speaks_hours():
    rec = _Rec(action_day=TODAY, days_until=0, gross_m3=500.0,
               plan=[_Plan()])
    s = water_section(rec, TODAY - timedelta(days=9), TODAY, pump=PUMP)
    assert "5 soat" in s.line  # 500 m3 / 100 m3/ч


def test_water_far_day_carries_date_stamp():
    day = TODAY + timedelta(days=4)
    rec = _Rec(action_day=day, days_until=4)
    s = water_section(rec, TODAY - timedelta(days=2), TODAY)
    assert s.status is Status.OK
    assert f"({day.day:02d}.{day.month:02d})" in s.line


def test_water_beyond_a_week_is_marked_as_an_estimate():
    """Тот же честный хедж, что и в /suv: дальше недели дата уточнится.
    Без него карточка утверждает день увереннее, чем сам движок."""
    far = _Rec(action_day=TODAY + timedelta(days=9), days_until=9)
    assert "taxminiy" in water_section(far, TODAY, TODAY).line
    assert "ориентировочно" in water_section(far, TODAY, TODAY,
                                             lang="ru").line

    near = _Rec(action_day=TODAY + timedelta(days=4), days_until=4)
    assert "taxminiy" not in water_section(near, TODAY, TODAY).line


def test_water_unknown_last_irrigation_says_taxminiy():
    s = water_section(_Rec(), None, TODAY)
    assert "taxminiy" in s.hint
    ru = water_section(_Rec(), None, TODAY, lang="ru")
    assert "приблизительный" in ru.hint


# ---------------------------------------------------------------- погода

def _day(t_max: float, rain: float = 0.0) -> DailyWeather:
    return DailyWeather(doy=200, t_max=t_max, t_min=20.0, rainfall=rain)


def test_weather_plain_day_is_ok():
    s = weather_section([_day(34.0), _day(33.0), _day(35.0)])
    assert s.status is Status.OK
    assert "+34°" in s.line
    assert "kutilmaydi" in s.line


def test_weather_heat_is_warn_and_rain_is_mentioned():
    s = weather_section([_day(42.0, rain=4.0), _day(38.0, rain=6.0)])
    assert s.status is Status.WARN
    assert "yomg'ir" in s.line  # 10 мм за окно — существенно

    assert weather_section([_day(39.9)]).status is Status.OK


def test_weather_without_forecast_is_honest_no_data():
    s = weather_section([])
    assert s.status is Status.NO_DATA
    assert "yo'q" in s.line


# ---------------------------------------------------------------- контур

def test_uniformity_is_not_drawn_for_drip():
    """ТЗ §3.2: секция неприменима — возвращает None и не рисуется совсем.
    Пустой слот «нет данных» у капельного поля был бы мусором, а не
    честностью: там вопрос «докуда добежала вода» не стоит."""
    assert uniformity_section("drip", None) is None
    assert uniformity_section("drip", 9.4) is None
    assert uniformity_section("sprinkler", 9.4) is None
    assert uniformity_section("furrow", None) is not None


def test_uniformity_without_contour_offers_to_draw():
    s = uniformity_section("furrow", None)
    assert s.status is Status.NO_DATA
    assert s.action is not None and s.action.callback == "fs:draw"
    assert "chegara" in (s.line + s.hint).lower()


def test_uniformity_with_contour_asks_for_the_inlet_side():
    """Контур есть — следующий недостающий шаг сторона входа воды:
    без неё не от чего строить ось борозды."""
    s = uniformity_section("furrow", 9.4)
    assert s.status is Status.NO_DATA
    assert "9,4 ga" in s.line
    assert "qaysi tomondan" in s.hint
    assert s.action is not None and s.action.callback == "fs:inlet"


def test_uniformity_with_inlet_asks_for_a_measurement():
    """Контур и сторона входа есть, замера нет: карту рисовать запрещено
    (§1.1), поэтому секция честно просит недостающий шаг."""
    s = uniformity_section("furrow", 9.4, inlet_side="shimol")
    assert s.status is Status.NO_DATA
    assert "9,4 ga" in s.line
    assert "shimol tomondan kiradi" in s.line
    assert "yetganini" in s.hint       # «докуда дошла вода»
    # Сторону входа можно переназначить: ошибиться в списке легко, а
    # обходить ради этого поле заново фермер не должен.
    assert s.action is not None and s.action.callback == "fs:inlet"
    assert "o'zgartirish" in s.action.label

    ru = uniformity_section("furrow", 9.4, inlet_side="север", lang="ru")
    assert "Вода заходит с севера" in ru.line


def test_uniformity_area_is_shown_to_one_decimal():
    """Три знака после запятой — это 10 м². Такой точности у обхода с
    телефоном нет, и фермер подтверждал другое число."""
    assert "8,7 ga" in uniformity_section("furrow", 8.707).line


def test_uniformity_flags_a_mismatch_with_the_declared_area():
    """Расчёт воды идёт по заявленной площади. Если обмер разошёлся с
    ней сильнее погрешности обхода, молчать нельзя: либо контур не тот,
    либо фермер годами поливает не тем объёмом."""
    s = uniformity_section("furrow", 9.4, declared_ha=6.0)
    assert "6,0 ga" in s.hint and "farq" in s.hint
    ru = uniformity_section("furrow", 9.4, declared_ha=6.0, lang="ru")
    assert "расхождение" in ru.hint

    # Небольшая разница — обычная погрешность GPS, о ней не кричим.
    quiet = uniformity_section("furrow", 9.4, declared_ha=9.0)
    assert "farq" not in (quiet.hint or "")


def test_uniformity_never_alarms_on_guesswork():
    """Пока нет замера, секция не имеет права красить поле в жёлтый или
    красный — иначе фермер пойдёт перекапывать поле по догадке."""
    for area in (None, 3.0, 50.0):
        assert uniformity_section("furrow", area).status is Status.NO_DATA


def test_uniformity_is_localized():
    ru = uniformity_section("furrow", 9.4, lang="ru")
    assert "9,4 га" in ru.line
    assert "Контур обведён" in ru.line


# ---------------------------------------------------------------- расходы

def test_cost_is_informational_and_never_colors_the_field():
    s = cost_section(4200.0, PUMP)
    assert s.status is Status.NO_DATA
    assert s.informational
    assert overall_status([_section(Status.OK), s]) is Status.OK


def test_cost_with_pump_shows_money_gravity_does_not():
    pumped = cost_section(4200.0, PUMP)
    assert "so'm" in pumped.line
    # 4200 m3 * 200 сум/m3 = 840 000, неразрывный пробел между тысячами
    assert "840 000" in pumped.line

    gravity = cost_section(4200.0, None)
    assert "so'm" not in gravity.line
    assert "4 200" in gravity.line


def test_cost_empty_season_is_no_data():
    assert "qayd etilmagan" in cost_section(0.0, PUMP).line


# ---------------------------------------------------------------- вёрстка

def _card_sections() -> list[Section]:
    rec = _Rec(action_day=TODAY, days_until=0, plan=[_Plan()])
    return assemble(
        water_section(rec, TODAY - timedelta(days=3), TODAY),
        weather_section([_day(34.0)]),
        cost_section(4200.0, PUMP),
    )


def test_card_leads_with_overall_status_in_words():
    card = render_card("Guliston", 10.0, "Paxta", _card_sections())
    lines = card.split("\n")
    assert lines[0] == "🌾 Guliston · 10 ga · Paxta"
    # Общий статус — первой строкой после шапки, словами, не только цветом.
    assert lines[2] == "🟡 E'tibor bering"
    assert "💧 Sug'orish · 🟡 e'tibor bering" in card
    assert "🌡 Ob-havo · 🟢 yaxshi" in card


def test_card_cost_section_has_no_status_word():
    card = render_card("Guliston", 10.0, "Paxta", _card_sections())
    assert "💸 Mavsum xarajati" in card
    assert "💸 Mavsum xarajati ·" not in card


def test_card_sections_are_at_most_two_lines():
    for block in render_card("G", 10.0, "Paxta",
                             _card_sections()).split("\n\n")[2:]:
        # заголовок секции + строка + опциональная подсказка
        assert len(block.split("\n")) <= 3


def test_russian_card_for_observer():
    rec = _Rec(action_day=TODAY, days_until=0, plan=[_Plan()])
    sections = assemble(
        water_section(rec, TODAY - timedelta(days=3), TODAY, lang="ru"),
        weather_section([_day(34.0)], lang="ru"),
        cost_section(4200.0, None, lang="ru"),
    )
    card = render_card("Guliston", 9.4, "Хлопок", sections, lang="ru")
    assert "🌾 Guliston · 9,4 га · Хлопок" in card
    assert "🟡 Обратите внимание" in card
    assert "самотёк" in card


def test_field_list_label_carries_status_emoji():
    label = field_list_label("Guliston", 10.0, "Paxta", Status.ALERT)
    assert label == "🔴 Guliston · 10 ga · Paxta"


def test_field_list_label_localizes_the_hectare_unit():
    label = field_list_label("Guliston", 10.0, "Хлопок", Status.OK, "ru")
    assert label == "🟢 Guliston · 10 га · Хлопок"


# ---------------------------------------------------------------- снимок

def _photo(ok=True, reason=None):
    from dataclasses import dataclass

    @dataclass
    class V:
        ok: bool
        reason: str | None
    return V(ok, reason)


def test_photo_section_does_not_care_how_the_field_is_irrigated():
    """Снимок — это замер, и способу полива он безразличен. Для капли
    карта даже нужнее: забитый капельник не видно никак, а сухое пятно
    вокруг него на снимке читается."""
    from suv.field_status import photo_section
    for method in ("drip", "furrow", "sprinkler"):
        s = photo_section(1.5, method, photo=_photo(ok=True))
        assert s is not None and s.action.callback == "fs:map"


def test_photo_section_invites_drawing_only_where_uniformity_does_not():
    """У борозды приглашение обвести уже стоит в секции равномерности —
    второй раз о том же не просим. У капли равномерности нет, значит
    зовёт снимок."""
    from suv.field_status import photo_section
    assert photo_section(None, "furrow") is None
    drip = photo_section(None, "drip")
    assert drip is not None and drip.action.callback == "fs:draw"


def test_photo_section_explains_why_there_is_no_map():
    from suv.field_status import photo_section
    s = photo_section(1.0, "drip", photo=_photo(ok=False, reason="too_small"))
    assert s.action is None
    assert "kichik" in s.line

    stale = photo_section(1.5, "drip",
                          photo=_photo(ok=False, reason="scene_stale"))
    assert "eski" in stale.line


def test_photo_section_never_colors_the_field():
    """Секция информационная: предлагает картинку, а не оценивает поле."""
    from suv.field_status import photo_section
    s = photo_section(1.5, "drip", photo=_photo(ok=True))
    assert s.status is Status.NO_DATA and s.informational
    assert overall_status([_section(Status.OK), s]) is Status.OK
