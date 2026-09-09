"""
Окна опрыскивания: когда химию не сдует и не смоет.

Правила — стандартные агрономические пороги над почасовым прогнозом,
никакой магии: ветер, температура, влажность воздуха и сухие часы после
обработки. Продукт говорит «когда удобно опрыскивать», а не «чем и от
чего» — рецептура остаётся агроному.

Пороги (документированные, консервативные):
* ветер 1–4 м/с на высоте штанги. Больше — снос на соседей; полный
  штиль — тоже плохо: утренняя инверсия держит облако мелких капель
  в воздухе, и куда оно поплывёт — неизвестно.
* температура 8–28 °C: в жару капля испаряется до цели и растут
  фитотоксические риски, в холод многие препараты не работают.
* относительная влажность от 40% — суше капля высыхает в полёте.
* после обработки нужно 4 сухих часа, иначе смоет; час с дождём
  непригоден сам по себе.
* окна ищем в светлое рабочее время (05:00–21:00 Ташкента) и не короче
  двух часов подряд — за час обработку не развернуть.

Как и весь погодный слой — это прогноз, не измерение: текст обязан
говорить «по прогнозу», а не обещать погоду.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .field_status import Section, Status
from .messages import WEEKDAY_RU, WEEKDAY_UZ
from .weather import HourlyWeather

WIND_MIN_MS = 1.0
WIND_MAX_MS = 4.0
TEMP_MIN_C = 8.0
TEMP_MAX_C = 28.0
RH_MIN = 40.0
DRY_HOURS_AFTER = 4      # часов без дождя после обработки
RAIN_EPS_MM = 0.2        # морось меньше этого сухости не отменяет
WORK_FROM_H = 5          # рабочее окно суток, часы местного времени
WORK_TO_H = 21
MIN_WINDOW_H = 2


@dataclass(frozen=True)
class SprayWindow:
    start: datetime
    end: datetime          # конец последнего пригодного часа (start+1h окна)
    wind_avg: float

    @property
    def hours(self) -> int:
        return int((self.end - self.start).total_seconds() // 3600)


def hour_ok(h: HourlyWeather, rain_ahead_mm: float) -> tuple[bool, str]:
    """Пригоден ли час. Вторым значением — ключ главной причины отказа,
    по нему строится честное «почему окна нет»."""
    if not WORK_FROM_H <= h.time.hour < WORK_TO_H:
        return False, "night"
    if h.rain_mm > RAIN_EPS_MM or rain_ahead_mm > RAIN_EPS_MM:
        return False, "rain"
    if h.wind_2m > WIND_MAX_MS:
        return False, "wind"
    if h.wind_2m < WIND_MIN_MS:
        return False, "calm"
    if h.temp > TEMP_MAX_C:
        return False, "heat"
    if h.temp < TEMP_MIN_C:
        return False, "cold"
    if h.rh < RH_MIN:
        return False, "dry_air"
    return True, ""


def windows(hours: list[HourlyWeather], limit: int = 3
            ) -> tuple[list[SprayWindow], str]:
    """Окна ≥ MIN_WINDOW_H подряд + главная причина, если окон нет.

    Причина — самый частый отказ в рабочие часы: «окна нет (ветер)»
    полезнее голого «окна нет».
    """
    out: list[SprayWindow] = []
    reasons: dict[str, int] = {}
    run: list[HourlyWeather] = []

    def close_run():
        nonlocal run
        if len(run) >= MIN_WINDOW_H:
            from datetime import timedelta
            out.append(SprayWindow(
                start=run[0].time, end=run[-1].time + timedelta(hours=1),
                wind_avg=sum(x.wind_2m for x in run) / len(run)))
        run = []

    for i, h in enumerate(hours):
        ahead = sum(x.rain_mm for x in hours[i + 1:i + 1 + DRY_HOURS_AFTER])
        ok, why = hour_ok(h, ahead)
        if ok:
            run.append(h)
        else:
            close_run()
            if why not in ("", "night"):
                reasons[why] = reasons.get(why, 0) + 1
    close_run()

    top = max(reasons, key=reasons.get) if reasons else ""
    return out[:limit], top


_REASON_UZ = {"wind": "shamol kuchli", "calm": "to'liq shtil (inversiya)",
              "heat": "jazirama issiq", "cold": "sovuq", "rain": "yomg'ir",
              "dry_air": "havo juda quruq"}
_REASON_RU = {"wind": "сильный ветер", "calm": "полный штиль (инверсия)",
              "heat": "жара", "cold": "холодно", "rain": "дождь",
              "dry_air": "слишком сухой воздух"}


def _day_word(d: datetime, today, lang: str) -> str:
    delta = (d.date() - today).days
    if delta == 0:
        return "bugun" if lang == "uz" else "сегодня"
    if delta == 1:
        return "ertaga" if lang == "uz" else "завтра"
    # WEEKDAY_RU — винительный падеж («субботу»), сам просит предлога.
    if lang == "uz":
        return f"{WEEKDAY_UZ[d.weekday()]} kuni"
    return f"в {WEEKDAY_RU[d.weekday()]}"


def _fmt(w: SprayWindow, today, lang: str) -> str:
    return (f"{_day_word(w.start, today, lang)} "
            f"{w.start:%H:%M}–{w.end:%H:%M} "
            f"(shamol ~{w.wind_avg:.0f} m/s)" if lang == "uz" else
            f"{_day_word(w.start, today, lang)} "
            f"{w.start:%H:%M}–{w.end:%H:%M} "
            f"(ветер ~{w.wind_avg:.0f} м/с)")


def build_section(hours: list[HourlyWeather] | None, today,
                  lang: str = "uz") -> Section | None:
    """Секция «Опрыскивание» для экрана Dala holati.

    None вместо ряда — почасовой прогноз не пришёл: секции нет вовсе,
    показывать заглушку хуже, чем промолчать (правило экрана: секция
    либо говорит правду, либо не выходит на сцену)."""
    if hours is None:
        return None
    uz = lang == "uz"
    title = "💨 Purkash" if uz else "💨 Опрыскивание"
    ws, top_reason = windows(hours)
    if not ws:
        why = (_REASON_UZ if uz else _REASON_RU).get(top_reason, "")
        tail = f" ({why})" if why else ""
        line = (f"48 soatda qulay oyna yo'q{tail} — prognoz bo'yicha."
                if uz else
                f"В ближайшие 48 ч окна нет{tail} — по прогнозу.")
        return Section(key="spray", order=35, title=title,
                       status=Status.WARN, line=line)
    first = _fmt(ws[0], today, lang)
    more = ""
    if len(ws) > 1:
        more = ("; keyingisi " if uz else "; следующее ") + \
            _fmt(ws[1], today, lang)
    line = ((f"Qulay: {first}{more}. Prognoz bo'yicha." if uz else
             f"Удобно: {first}{more}. По прогнозу."))
    return Section(key="spray", order=35, title=title,
                   status=Status.OK, line=line)
