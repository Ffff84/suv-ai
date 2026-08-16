"""
Экран «Dala holati» — реестр секций и вёрстка карточки.

Карточка собирается не хардкодом, а из секций: заморозки, засоление и
болезни потом встанут в тот же экран без переделки. Каждая секция — это
эмодзи, словесный статус (никогда только цвет: часть фермеров не
различает красный и зелёный) и максимум две строки текста.

Ядро чистое: ноль зависимостей от telegram, вся сборка данных — в боте.
По ТЗ модуль назывался core/field_status.py; в этом репо роль «core»
играет пакет suv/, второй пакет для того же — лишняя сущность.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import IntEnum
from typing import Iterable

from .economics import PumpProfile, cost_of
from .et0 import DailyWeather
from .messages import WEEKDAY_RU, WEEKDAY_UZ


class Status(IntEnum):
    NO_DATA = 0  # не влияет на общий статус поля
    OK = 1
    WARN = 2
    ALERT = 3


@dataclass(frozen=True)
class Action:
    """Кнопка drill-down секции. В первой итерации не используется,
    но реестр обязан её уметь: пустые слоты (контур, замер) ведут
    фермера к недостающему шагу именно кнопкой."""

    label: str
    callback: str


@dataclass
class Section:
    key: str               # "water" | "uniformity" | "weather" | "cost"
    order: int             # 10, 20, 30, 40 — фиксированный порядок
    title: str             # локализованный, с эмодзи
    status: Status
    line: str              # одна строка
    hint: str | None = None       # вторая строка, опционально
    action: Action | None = None  # кнопка drill-down
    # Информационная секция (расходы): показывает цифры, а не оценку,
    # поэтому словесного статуса у неё нет — ср. макет карточки в ТЗ.
    informational: bool = False


STATUS_EMOJI = {Status.NO_DATA: "⚪️", Status.OK: "🟢",
                Status.WARN: "🟡", Status.ALERT: "🔴"}

STATUS_WORD = {
    "uz": {Status.NO_DATA: "ma'lumot yo'q", Status.OK: "yaxshi",
           Status.WARN: "e'tibor bering", Status.ALERT: "muammo"},
    "ru": {Status.NO_DATA: "нет данных", Status.OK: "хорошо",
           Status.WARN: "внимание", Status.ALERT: "проблема"},
}

OVERALL_HEADER = {
    "uz": {Status.NO_DATA: "⚪️ Ma'lumot hali kam",
           Status.OK: "🟢 Hammasi yaxshi",
           Status.WARN: "🟡 E'tibor bering",
           Status.ALERT: "🔴 E'tibor kerak"},
    "ru": {Status.NO_DATA: "⚪️ Данных пока мало",
           Status.OK: "🟢 Всё хорошо",
           Status.WARN: "🟡 Обратите внимание",
           Status.ALERT: "🔴 Требует внимания"},
}

LIST_HEADER = {"uz": "Qaysi dalani ko'ramiz?", "ru": "Какое поле смотрим?"}


def _num(v: float) -> str:
    """Неразрывный пробел как разделитель тысяч — как в messages.py."""
    return f"{v:,.0f}".replace(",", " ")


def _ha(hectares: float) -> str:
    return f"{hectares:g}".replace(".", ",")


def overall_status(sections: Iterable[Section]) -> Status:
    """Общий статус поля = максимум по секциям. NO_DATA в расчёт не
    идёт: незаполненный слот не делает поле красным."""
    known = [s.status for s in sections if s.status != Status.NO_DATA]
    return max(known) if known else Status.NO_DATA


def assemble(*sections: Section | None) -> list[Section]:
    """Отбросить неприменимые (None) и выстроить по фиксированному
    порядку. None — штатный ответ модуля: равномерность при капельном
    поливе не рисуется совсем."""
    return sorted((s for s in sections if s is not None),
                  key=lambda s: s.order)


# ---------------------------------------------------------------- секции

# Порог «планирующей» даты — тот же, что в messages.py: дальше недели
# прогноз ещё уточнится, и обещать точный день нечестно.
FAR_DATE_DAYS = 7


def water_section(rec, last_irr: date | None, today: date,
                  pump: PumpProfile | None = None,
                  lang: str = "uz") -> Section:
    """Секция полива поверх готовой рекомендации FAO-56 движка.

    Совет здесь ОБЯЗАН совпадать с тем, что фермер получает по кнопке
    «Suv holati» — поэтому секция не считает ничего сама, а только
    переводит Recommendation в строку статуса.
    """
    uz = lang == "uz"
    title = "💧 Sug'orish" if uz else "💧 Полив"

    hours = None
    if pump is not None and getattr(pump, "m3_per_hour", 0):
        hours = rec.gross_m3 / pump.m3_per_hour

    if rec.action_day is None:
        status = Status.OK
        line = "Bu hafta shart emas." if uz else "На этой неделе не требуется."
    elif rec.days_until <= 0:
        stressed = bool(rec.plan) and rec.plan[0].stress
        status = Status.ALERT if stressed else Status.WARN
        if uz:
            line = ("Suv kechikmoqda — bugun sug'oring." if stressed
                    else "Bugun sug'oring.")
            if hours is not None:
                line = line[:-1] + f", nasos ~{hours:.0f} soat."
        else:
            line = ("Влага ниже порога — полейте сегодня." if stressed
                    else "Полейте сегодня.")
            if hours is not None:
                line = line[:-1] + f", насос ~{hours:.0f} ч."
    elif rec.days_until == 1:
        status = Status.WARN
        line = "Ertaga sug'oring." if uz else "Полив завтра."
    else:
        status = Status.OK
        d = rec.action_day
        stamp = f"({d.day:02d}.{d.month:02d})"
        line = (f"Keyingi: {WEEKDAY_UZ[d.weekday()]} kuni {stamp}" if uz
                else f"Следующий: в {WEEKDAY_RU[d.weekday()]} {stamp}")
        # Дальше недели дата — планирующая оценка и законно съезжает на
        # день. messages.py говорит об этом прямо; карточка обязана
        # говорить то же, иначе два экрана бота противоречат друг другу.
        if rec.days_until >= FAR_DATE_DAYS:
            line += " · taxminiy" if uz else " · ориентировочно"

    if last_irr is None:
        # Тот же честный компромисс, что и в рекомендации: без якоря
        # расчёт приблизительный, и молчать об этом нельзя.
        hint = ("Oxirgi sug'orish sanasi noma'lum — hisob taxminiy." if uz
                else "Дата последнего полива неизвестна — расчёт приблизительный.")
    else:
        ago = (today - last_irr).days
        if uz:
            when = "bugun" if ago <= 0 else "kecha" if ago == 1 else f"{ago} kun oldin"
            hint = f"Oxirgi: {when}"
        else:
            when = "сегодня" if ago <= 0 else "вчера" if ago == 1 else f"{ago} дн. назад"
            hint = f"Последний: {when}"

    return Section(key="water", order=10, title=title,
                   status=status, line=line, hint=hint)


HEAT_WARN_C = 40.0     # выше — жара, испарение резко растёт
RAIN_MENTION_MM = 5.0  # меньше — для полива несущественно, не упоминаем


def weather_section(forecast: list[DailyWeather],
                    lang: str = "uz") -> Section:
    """Погода на ближайшие дни. Прогноз может не прийти — тогда честный
    пустой слот, а не молчание и не вчерашние цифры."""
    uz = lang == "uz"
    title = "🌡 Ob-havo" if uz else "🌡 Погода"

    if not forecast:
        return Section(
            key="weather", order=30, title=title, status=Status.NO_DATA,
            line=("Ob-havo ma'lumoti hozircha yo'q." if uz
                  else "Данных о погоде пока нет."))

    t = round(forecast[0].t_max)
    t_str = f"+{t}" if t > 0 else str(t)
    days = min(3, len(forecast))
    rain = sum(d.rainfall for d in forecast[:days])

    if rain >= RAIN_MENTION_MM:
        line = (f"Bugun {t_str}°, {days} kun ichida yomg'ir ~{rain:.0f} mm"
                if uz else
                f"Сегодня {t_str}°, за {days} дн. дождь ~{rain:.0f} мм")
    else:
        line = (f"Bugun {t_str}°, yomg'ir kutilmaydi" if uz
                else f"Сегодня {t_str}°, дождя не ожидается")

    status, hint = Status.OK, None
    if forecast[0].t_max >= HEAT_WARN_C:
        status = Status.WARN
        hint = ("Jazirama — bug'lanish kuchli." if uz
                else "Сильная жара — испарение повышено.")

    return Section(key="weather", order=30, title=title,
                   status=status, line=line, hint=hint)


DRAW_ACTION_UZ = "🗺 Dalani chizish"
DRAW_ACTION_RU = "🗺 Обвести поле"
REDRAW_ACTION_UZ = "🗺 Qayta chizish"
REDRAW_ACTION_RU = "🗺 Обвести заново"
INLET_ACTION_UZ = "💧 Suv qaysi tomondan?"
INLET_ACTION_RU = "💧 Откуда заходит вода?"
INLET_CHANGE_UZ = "💧 Suv tomonini o'zgartirish"
INLET_CHANGE_RU = "💧 Изменить сторону входа"
MAP_ACTION_UZ = "🗺 Xaritani ko'rish"
MAP_ACTION_RU = "🗺 Показать карту"

# Почему карты нет — по одной короткой строке на причину (ТЗ §4.6).
# Пара: узбекский, русский.
PHOTO_BLOCKED = {
    "too_small": ("Dala 3 gektardan kichik — sun'iy yo'ldosh zonalarni "
                  "ajrata olmaydi",
                  "Поле меньше 3 га — спутник не различит зоны"),
    "no_scene": ("Sun'iy yo'ldosh surati hali yo'q",
                 "Снимка со спутника пока нет"),
    "scene_stale": ("Yangi surat yo'q — oxirgi surat juda eski",
                    "Свежего снимка нет — последний слишком старый"),
    "clouded": ("Dala bulut ostida qoldi — surat chiqmadi",
                "Поле закрыто облаками — снимок не получился"),
}

# Заявленная и обмеренная площади всегда расходятся немного: фермер
# помнит по документам, обмер идёт по телефонному GPS. Молчать стоит,
# пока расхождение в пределах десятой части — дальше это уже не
# погрешность, а повод перепроверить контур.
AREA_MISMATCH_FRAC = 0.10


def _ha1(value: float) -> str:
    """Гектары с одним знаком. Третий знак — это 10 м², точность, которой
    у обхода с телефоном нет, и фермер подтверждал другое число."""
    return f"{value:.1f}".replace(".", ",")


def uniformity_section(irrigation_method: str, area_ha: float | None,
                       has_reach: bool = False,
                       lang: str = "uz",
                       declared_ha: float | None = None,
                       inlet_side: str | None = None,
                       photo=None) -> Section | None:
    """Равномерность полива. None = секция неприменима и не рисуется.

    Расчёт движения воды по борозде (модуль Egat) осмыслен только для
    бороздкового полива: при капельном вода подаётся в каждый куст, при
    дождевании — сверху, и «докуда добежала» там не вопрос. Пустой слот
    в таких карточках был бы не честностью, а мусором.

    Пока это только пустые состояния из ТЗ §3.5: сам расчёт приходит с
    модулем Egat, а карта — с замером. Рисовать «вода не дошла» по
    нормативам запрещено (§1.1), поэтому статус здесь никогда не хуже
    NO_DATA — секция не пугает фермера догадкой.
    """
    if irrigation_method != "furrow":
        return None

    uz = lang == "uz"
    title = "📐 Sug'orish tekisligi" if uz else "📐 Равномерность полива"

    if not area_ha:
        return Section(
            key="uniformity", order=20, title=title, status=Status.NO_DATA,
            line=("Dalangiz chegarasini chizsang, suv" if uz
                  else "Обведите поле — покажу, куда"),
            hint=("qayerga yetayotganini ko'rsataman" if uz
                  else "вода доходит, а куда нет"),
            action=Action(DRAW_ACTION_UZ if uz else DRAW_ACTION_RU, "fs:draw"))

    drawn = (f"Chegara chizilgan: {_ha1(area_ha)} ga" if uz
             else f"Контур обведён: {_ha1(area_ha)} га")
    # Контур можно перечертить: угол, отмеченный не там, иначе остаётся
    # в базе навсегда, и кнопки исправить его нет нигде.
    redraw = Action(REDRAW_ACTION_UZ if uz else REDRAW_ACTION_RU, "fs:draw")

    if photo is not None and photo.reason in PHOTO_BLOCKED:
        # Фото не построить — говорим одной строкой почему (ТЗ §4.6).
        # Кнопки карты при этом нет: мёртвая кнопка хуже её отсутствия.
        why = PHOTO_BLOCKED[photo.reason]
        return Section(
            key="uniformity", order=20, title=title, status=Status.NO_DATA,
            line=drawn,
            hint=why[0] if uz else why[1],
            action=redraw if photo.reason == "too_small" else
            Action(INLET_ACTION_UZ if uz else INLET_ACTION_RU, "fs:inlet")
            if inlet_side is None else redraw)

    if declared_ha and abs(area_ha - declared_ha) / declared_ha >= AREA_MISMATCH_FRAC:
        # Две площади разошлись сильнее погрешности обхода. Молча выбрать
        # одну нельзя: расчёт воды идёт по заявленной, и если верна
        # обмеренная, фермер поливает не тем объёмом.
        return Section(
            key="uniformity", order=20, title=title, status=Status.NO_DATA,
            line=drawn,
            hint=(f"Ro'yxatda {_ha1(declared_ha)} ga — farq bor, tekshiring"
                  if uz else
                  f"В анкете {_ha1(declared_ha)} га — есть расхождение"),
            action=redraw)

    if photo is not None and photo.ok:
        # Снимок есть — он и есть главное, что фермеру тут нужно. Кнопка
        # карты вытесняет вопрос о стороне входа: замер влажности берётся
        # по пикселям и в направлении борозды не нуждается.
        #
        # Про сторону входа тут молчим: подсказка «можно ещё указать»
        # висела без кнопки, а нажать было негде — шаг становился
        # недостижимым ровно тогда, когда карточка о нём напоминала.
        return Section(
            key="uniformity", order=20, title=title, status=Status.NO_DATA,
            line=drawn if inlet_side is None else
            (f"{drawn} · Suv {inlet_side} tomondan kiradi" if uz
             else f"{drawn} · Вода заходит с {inlet_side}а"),
            action=Action(MAP_ACTION_UZ if uz else MAP_ACTION_RU, "fs:map"))

    if inlet_side is None:
        # Следующий недостающий шаг — сторона входа воды. Без неё ось
        # борозды не построить: заливка растёт от того края, где вода
        # заходит, и «откуда» тут не деталь, а начало отсчёта.
        return Section(
            key="uniformity", order=20, title=title, status=Status.NO_DATA,
            line=drawn,
            hint=("Suv dalaga qaysi tomondan kiradi?" if uz
                  else "С какой стороны вода заходит на поле?"),
            action=Action(INLET_ACTION_UZ if uz else INLET_ACTION_RU,
                          "fs:inlet"))

    entered = (f"Suv {inlet_side} tomondan kiradi" if uz
               else f"Вода заходит с {inlet_side}а")
    # Сторону можно переназначить: ошибиться в списке легко, а обходить
    # ради этого поле заново — цена, которой фермер платить не должен.
    # Перечертить контур предлагается уже на самом экране выбора, чтобы
    # под карточкой не разрасталась клавиатура.
    redraw = Action(INLET_CHANGE_UZ if uz else INLET_CHANGE_RU, "fs:inlet")

    if not has_reach:
        # Контур и сторона входа есть, замера нет. Карту рисовать
        # запрещено (§1.1): пока не измерено, докуда вода дошла,
        # любая заливка — догадка, а догадка убедительнее данных.
        return Section(
            key="uniformity", order=20, title=title, status=Status.NO_DATA,
            line=f"{drawn} · {entered}",
            hint=("Suv qayergacha yetganini belgilasang, tekislikni ko'rsataman"
                  if uz else
                  "Отметьте, докуда дошла вода — покажу равномерность"),
            action=redraw)

    return Section(
        key="uniformity", order=20, title=title, status=Status.NO_DATA,
        line=f"{drawn} · {entered}", action=redraw)


def cost_section(season_m3: float, pump: PumpProfile | None,
                 lang: str = "uz") -> Section:
    """Расходы сезона: подтверждённый объём × замеренная характеристика
    насоса. Самотёку деньги не показываем — вода фермеру ничего не
    стоит, и «сэкономили сумы» там неправда (см. economics.py).

    Секция информационная: цифры, а не оценка, поэтому она никогда не
    красит общий статус поля.
    """
    uz = lang == "uz"
    title = "💸 Mavsum xarajati" if uz else "💸 Расходы за сезон"

    if season_m3 <= 0:
        return Section(
            key="cost", order=40, title=title, status=Status.NO_DATA,
            line=("Hali sug'orish qayd etilmagan." if uz
                  else "Поливов пока не отмечено."),
            informational=True)

    if pump is None:
        line = (f"Suv {_num(season_m3)} m³ · kanaldan, elektr sarfi yo'q"
                if uz else
                f"Вода {_num(season_m3)} м³ · самотёк, электричество не расходуется")
    else:
        c = cost_of(season_m3, pump)
        line = (f"Suv {_num(c.m3)} m³ · elektr {_num(c.uzs)} so'm" if uz
                else f"Вода {_num(c.m3)} м³ · электричество {_num(c.uzs)} сум")

    return Section(key="cost", order=40, title=title, status=Status.NO_DATA,
                   line=line, informational=True)


# ---------------------------------------------------------------- вёрстка

def _section_block(s: Section, lang: str) -> str:
    head = s.title if s.informational else (
        f"{s.title} · {STATUS_EMOJI[s.status]} {STATUS_WORD[lang][s.status]}")
    lines = [head, s.line]
    if s.hint:
        lines.append(s.hint)
    return "\n".join(lines)


def _ga(lang: str) -> str:
    return "ga" if lang == "uz" else "га"


def render_card(name: str, hectares: float, crop_name: str,
                sections: list[Section], lang: str = "uz") -> str:
    """Карточка поля: общий статус первой строкой, секции по порядку."""
    overall = overall_status(sections)
    parts = [f"🌾 {name} · {_ha(hectares)} {_ga(lang)} · {crop_name}",
             OVERALL_HEADER[lang][overall]]
    parts.extend(_section_block(s, lang) for s in sections)
    return "\n\n".join(parts)


def field_list_label(name: str, hectares: float, crop_name: str,
                     status: Status, lang: str = "uz") -> str:
    """Строка поля в списке: статус эмодзи + имя + площадь + культура."""
    return (f"{STATUS_EMOJI[status]} {name} · "
            f"{_ha(hectares)} {_ga(lang)} · {crop_name}")
