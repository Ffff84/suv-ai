"""
Farmer-facing message templates, Uzbek first.

The rule that governs this file: ONE instruction, ONE reason, ONE number.
No tables, no ranges, no hedging. A farmer reading this on a cracked
phone in a field at 6am must be able to act without re-reading.

Uzbek is the primary language because that is the moat. Russian is
provided for cluster agronomists and ministry dashboards, who are a
different audience with different needs.
"""

from __future__ import annotations

from datetime import date

WEEKDAY_UZ = ("Dushanba", "Seshanba", "Chorshanba", "Payshanba",
              "Juma", "Shanba", "Yakshanba")
# Accusative case — Russian says "в субботу", not "в суббота".
WEEKDAY_RU = ("понедельник", "вторник", "среду", "четверг",
              "пятницу", "субботу", "воскресенье")

REASON_UZ = {
    "threshold_reached": "Tuproqdagi namlik chegaraga yetdi.",
    "threshold_approaching": "Namlik tez kamaymoqda.",
    "after_rain": "Yomg'irdan keyin namlik yana kamayadi.",
    "rain_expected": "Yaqin kunlarda yomg'ir kutilmoqda.",
    "soil_still_wet": "Tuproq hali yetarlicha nam.",
}
REASON_RU = {
    "threshold_reached": "Влагозапас достиг порога.",
    "threshold_approaching": "Влага убывает быстро.",
    "after_rain": "После дождя влага снова снизится.",
    "rain_expected": "В ближайшие дни ожидается дождь.",
    "soil_still_wet": "Почва ещё достаточно влажная.",
}


def _wd(d: date, lang: str) -> str:
    return (WEEKDAY_UZ if lang == "uz" else WEEKDAY_RU)[d.weekday()]


def _num(v: float) -> str:
    """Thin space as the thousands separator, the way both languages
    print large numbers. Applied to the NUMBER only — running it over the
    whole message eats the sentence commas."""
    return f"{v:,.0f}".replace(",", "\u00a0")


def recommendation_text(rec, lang: str = "uz", pump=None) -> str:
    """
    The message that actually goes out.

    When the field is pumped, the instruction is given in HOURS, not
    millimetres or cubic metres. That came straight from the first pilot
    farmer: asked how much water he applies, he could not answer in m3 —
    but he knows exactly how long he leaves the pump running, because
    that is the control he actually operates and the thing his
    electricity bill is made of.

    Millimetres are the engine's unit. Hours are the farmer's.
    """
    f = rec.field
    if rec.action_day is None:
        if lang == "uz":
            return (f"{f.name}\n\n"
                    f"Bu hafta sug'orish shart emas.\n"
                    f"{REASON_UZ.get(rec.reason_key, '')}\n\n"
                    f"Keyingi tekshiruv — 3 kundan keyin.")
        return (f"{f.name}\n\n"
                f"На этой неделе полив не требуется.\n"
                f"{REASON_RU.get(rec.reason_key, '')}\n\n"
                f"Следующая проверка — через 3 дня.")

    hours = None
    if pump is not None and getattr(pump, "m3_per_hour", 0):
        hours = rec.gross_m3 / pump.m3_per_hour

    day = rec.action_day
    # Дата в скобках обязательна: в 14-дневном окне прогноза каждый день
    # недели встречается дважды, и «в четверг» без даты не отличает
    # «через 4 дня» от «через 11». Нашлось, когда фермер сравнил ответы
    # за два дня подряд и решил, что бот противоречит сам себе.
    when_uz = "Bugun" if rec.days_until == 0 else (
        "Ertaga" if rec.days_until == 1
        else f"{_wd(day, 'uz')} kuni ({day.day:02d}.{day.month:02d})")
    when_ru = "Сегодня" if rec.days_until == 0 else (
        "Завтра" if rec.days_until == 1
        else f"В {_wd(day, 'ru')} ({day.day:02d}.{day.month:02d})")

    # Дата дальше недели — планирующая оценка: прогноз и снимки
    # обновляются ежедневно, и она законно уточняется на ±1 день.
    # Говорим об этом прямо — иначе вчерашняя «суббота» против
    # сегодняшнего «воскресенья» читается как противоречие бота.
    far_uz = "\nSana yaqinlashganda aniqlashadi." if rec.days_until >= 7 else ""
    far_ru = "\nДата уточнится по мере приближения." if rec.days_until >= 7 else ""

    if lang == "uz":
        head = (f"{when_uz} nasosni {hours:.0f} soat ishlating."
                if hours is not None
                else f"{when_uz}, {rec.gross_mm:.0f} mm.")
        tail = (f"Taxminan {_num(rec.gross_m3)} m³ suv "
                f"({f.hectares:.1f} ga).")
        if hours is not None and getattr(pump, "cost_per_hour_uzs", 0):
            tail += (f"\nElektr uchun taxminan "
                     f"{_num(hours * pump.cost_per_hour_uzs)} so'm.")
        return (f"{f.name}\n\n{head}\n{REASON_UZ.get(rec.reason_key, '')}"
                f"{far_uz}\n\n{tail}")

    head = (f"{when_ru} включите насос на {hours:.0f} ч."
            if hours is not None
            else f"{when_ru}, {rec.gross_mm:.0f} мм.")
    tail = (f"Примерно {_num(rec.gross_m3)} м³ воды "
            f"({f.hectares:.1f} га).")
    if hours is not None and getattr(pump, "cost_per_hour_uzs", 0):
        tail += (f"\nЭлектричество — около "
                 f"{_num(hours * pump.cost_per_hour_uzs)} сум.")
    return (f"{f.name}\n\n{head}\n{REASON_RU.get(rec.reason_key, '')}"
            f"{far_ru}\n\n{tail}")


def salinity_warning(level: str, lang: str = "uz") -> str | None:
    """Only ever shown at 'high'. A warning on every message is noise."""
    if level != "high":
        return None
    if lang == "uz":
        return ("Diqqat: sizning dalangizda sizot suvi yuqori. "
                "Ortiqcha sug'orish tuproqni sho'rlantiradi.")
    return ("Внимание: на вашем поле высокий уровень грунтовых вод. "
            "Избыточный полив приведёт к засолению.")


def savings_text(summary, lang: str = "uz") -> str:
    if lang == "uz":
        v = "Tasdiqlangan" if summary.verified else "Fermer ma'lumoti"
        return (f"Mavsum boshidan: {_num(summary.saved_m3)} m³ suv tejaldi.\n"
                f"Manba: {v}.")
    v = "Подтверждено счётчиком" if summary.verified else "Со слов фермера"
    return (f"С начала сезона сэкономлено {_num(summary.saved_m3)} м³.\n"
            f"Источник: {v}.")
