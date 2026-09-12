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
    "harvest_hold": "Terim davri: suvga to'lgan meva omborda yomon saqlanadi.",
    "preharvest_hold": "Terim oldidan quruq tanaffus: meva omborda yaxshi turadi.",
    "threshold_reached": "Tuproqdagi namlik chegaraga yetdi.",
    "threshold_approaching": "Namlik tez kamaymoqda.",
    "after_rain": "Yomg'irdan keyin namlik yana kamayadi.",
    "rain_expected": "Yaqin kunlarda yomg'ir kutilmoqda.",
    "soil_still_wet": "Tuproq hali yetarlicha nam.",
    "season_over": "Ang'iz bo'yicha suv hisoblanmaydi.",
}
REASON_RU = {
    "harvest_hold": "Идёт съём: налитый водой плод хуже лежит в хранении.",
    "preharvest_hold": "Сухая пауза перед съёмом: плод лучше лежит в хранении.",
    "threshold_reached": "Влагозапас достиг порога.",
    "threshold_approaching": "Влага убывает быстро.",
    "after_rain": "После дождя влага снова снизится.",
    "rain_expected": "В ближайшие дни ожидается дождь.",
    "soil_still_wet": "Почва ещё достаточно влажная.",
    "season_over": "По убранному полю воду не считаем.",
}


def _wd(d: date, lang: str) -> str:
    return (WEEKDAY_UZ if lang == "uz" else WEEKDAY_RU)[d.weekday()]


def _num(v: float) -> str:
    """Thin space as the thousands separator, the way both languages
    print large numbers. Applied to the NUMBER only — running it over the
    whole message eats the sentence commas."""
    return f"{v:,.0f}".replace(",", "\u00a0")


def snapshot_line(rec, lang: str = "uz") -> str:
    """Возраст и источник числа, на котором стоит совет.

    Без этой строки фермер не отличает совет по свежему снимку от совета
    по чистому календарю: сообщение выглядит одинаково уверенным и в том,
    и в другом случае. Статус спутника до сих пор уходил только в
    log.info — то есть виден был нам и не виден тому, кто по нему
    поливает.
    """
    d = getattr(rec.field, "ndvi_date", None)
    if d is None:
        return ("Surat yo'q — hisob kalendar bo'yicha." if lang == "uz"
                else "Снимка нет — расчёт по календарю.")
    age = (rec.generated_on - d).days
    if lang == "uz":
        when = "bugungi" if age <= 0 else ("kechagi" if age == 1
                                           else f"{age} kun oldingi")
        return f"Sun'iy yo'ldosh surati — {when}."
    when = "сегодняшний" if age <= 0 else ("вчерашний" if age == 1
                                           else f"{age} дн. назад")
    return f"Снимок со спутника — {when}."


def recommendation_text(rec, lang: str = "uz", pump=None) -> str:
    """
    The message that actually goes out.

    When the field is pumped, the instruction is given in HOURS, not
    millimetres or cubic metres. That came straight from the first pilot
    farmer: asked how much water he applies, he could not answer in m3 —
    but he knows exactly how long he leaves the pump running, because
    that is the control he actually operates and the thing his
    electricity bill is made of.

    Millimetres are the engine's unit. Hours are the farmer's — and on a
    gravity-fed field, where there is no pump and no hours to give, the
    farmer's unit is cubic metres per hectare, the one the water
    authority also writes his limit in. Millimetres never leave the
    engine: they live in the cabinet and in the log.
    """
    f = rec.field
    snap = snapshot_line(rec, lang)
    if rec.reason_key == "season_over":
        # Не «полив не требуется»: требоваться нечему. Раньше поле с
        # кончившимся сезоном получало ровно ту же успокаивающую строку,
        # что и живое влажное поле, — с Kc на хвосте кривой и нулём
        # кубометров, будто расчёт состоялся.
        sown = f.planting_date
        when = f"{sown.day:02d}.{sown.month:02d}.{sown.year}"
        if lang == "uz":
            return (f"{f.name}\n\n"
                    f"{f.crop.name_uz} mavsumi tugadi (ekilgan {when}).\n"
                    f"{REASON_UZ['season_over']}\n\n"
                    f"Qayta ekkan bo'lsangiz — yozing, dalani yangi "
                    f"mavsumga sozlaymiz.")
        return (f"{f.name}\n\n"
                f"Сезон культуры «{f.crop.name_ru}» закончился "
                f"(сев {when}).\n"
                f"{REASON_RU['season_over']}\n\n"
                f"Посеяли заново — напишите, переведём поле на новый сезон.")
    if rec.reason_key == "harvest_hold":
        # До начала съёма говорить «идёт съём» нельзя: дата лежит в том
        # же объекте и опровергает фразу. У сада Фарруха 12.09 съём
        # назначен на 14.09 — идёт сухая пауза, а не терим.
        hs = f.harvest_start
        before = hs is not None and rec.generated_on < hs
        # Не «не требуется» — влага может быть у порога. Полив
        # ОСТАНОВЛЕН сознательно, и фермер должен видеть разницу.
        key = "preharvest_hold" if before else "harvest_hold"
        when = f"{hs.day:02d}.{hs.month:02d}" if hs else ""
        # Конец съёма фермер не называл, а без него пауза держится
        # ограниченный срок: просим сказать, а не обещаем за него.
        if lang == "uz":
            head = (f"Terim oldidan sug'orish to'xtatildi (terim — {when} dan)."
                    if before else "Terim davri — sug'orish to'xtatilgan.")
            return (f"{f.name}\n\n{head}\n{REASON_UZ[key]}\n\n"
                    "Terimni tugatgach yozing — maslahatlar qaytadi: bog' "
                    "keyingi mavsum uchun suv ichishi kerak.")
        head = (f"Полив остановлен перед съёмом (съём с {when})."
                if before else "Съём урожая — полив приостановлен.")
        return (f"{f.name}\n\n{head}\n{REASON_RU[key]}\n\n"
                "Напишите, когда закончите съём, — советы вернутся: саду "
                "нужен послеуборочный полив под будущие почки.")
    if rec.action_day is None:
        if lang == "uz":
            return (f"{f.name}\n\n"
                    f"Bu hafta sug'orish shart emas.\n"
                    f"{REASON_UZ.get(rec.reason_key, '')}\n\n"
                    f"Keyingi tekshiruv — ertaga ertalab.\n{snap}")
        return (f"{f.name}\n\n"
                f"На этой неделе полив не требуется.\n"
                f"{REASON_RU.get(rec.reason_key, '')}\n\n"
                f"Следующая проверка — завтра утром.\n{snap}")

    hours = None
    if pump is not None and getattr(pump, "m3_per_hour", 0):
        # Округляем ДО расчёта денег. Фермеру говорят целые часы, и
        # стоимость обязана сходиться именно с ними: инструкция «10
        # soat» при цене за 9.6 часа читалась как ошибка бота в деньгах
        # — Фаррух цену часа знает наизусть, это его собственный замер.
        hours = round(rec.gross_m3 / pump.m3_per_hour)

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

    # Поле без насоса — самотёк. Миллиметры там были единицей движка, а
    # не фермера: поливная норма, в которой думают и дехканин, и водхоз,
    # меряется в кубах на гектар (600-1200). Часы подачи для самотёка не
    # выдумываем — расход канала в л/с мы не знаем и оценить не можем.
    per_ha = rec.gross_m3 / f.hectares if f.hectares else 0.0

    if lang == "uz":
        head = (f"{when_uz} nasosni {hours:.0f} soat ishlating."
                if hours is not None
                else f"{when_uz} sug'oring, gektariga {_num(per_ha)} m³.")
        tail = (f"Taxminan {_num(rec.gross_m3)} m³ suv "
                f"({f.hectares:.1f} ga).")
        if hours is not None and getattr(pump, "cost_per_hour_uzs", 0):
            tail += (f"\nElektr uchun taxminan "
                     f"{_num(hours * pump.cost_per_hour_uzs)} so'm.")
        return (f"{f.name}\n\n{head}\n{REASON_UZ.get(rec.reason_key, '')}"
                f"{far_uz}\n\n{tail}\n{snap}")

    head = (f"{when_ru} включите насос на {hours:.0f} ч."
            if hours is not None
            else f"{when_ru} полейте, {_num(per_ha)} м³ на гектар.")
    tail = (f"Примерно {_num(rec.gross_m3)} м³ воды "
            f"({f.hectares:.1f} га).")
    if hours is not None and getattr(pump, "cost_per_hour_uzs", 0):
        tail += (f"\nЭлектричество — около "
                 f"{_num(hours * pump.cost_per_hour_uzs)} сум.")
    return (f"{f.name}\n\n{head}\n{REASON_RU.get(rec.reason_key, '')}"
            f"{far_ru}\n\n{tail}\n{snap}")


def salinity_warning(level: str, lang: str = "uz") -> str | None:
    """Only ever shown at 'high'. A warning on every message is noise."""
    if level != "high":
        return None
    if lang == "uz":
        return ("Diqqat: sizning dalangizda sizot suvi yuqori. "
                "Ortiqcha sug'orish tuproqni sho'rlantiradi.")
    return ("Внимание: на вашем поле высокий уровень грунтовых вод. "
            "Избыточный полив приведёт к засолению.")


def why_text(rec, last_irr, lang: str = "uz", degraded: bool = False) -> str:
    """«Почему такой совет» — детерминированное объяснение из уже
    посчитанной рекомендации. Никакой генерации: каждая строка — число
    из расчёта, которое можно проверить по журналу. Это сознательная
    альтернатива AI-чату: объяснение не умеет приукрасить.
    """
    uz = lang == "uz"
    p = rec.plan[0] if rec.plan else None
    lines = [rec.field.name, ""]

    if p is not None:
        if uz:
            lines.append(f"Tuproq zaxirasi: {p.depletion_mm:.0f} mm sarflangan, "
                         f"chegara — {p.raw_mm:.0f} mm.")
            lines.append(f"Bug'lanish: kuniga ~{p.etc_mm:.1f} mm "
                         f"(ET0 {p.et0_mm:.1f} × Kc {p.kc:g}).")
            lines.append(f"Kc manbai: {p.kc_source}.")
        else:
            lines.append(f"Запас влаги: израсходовано {p.depletion_mm:.0f} мм "
                         f"из порога {p.raw_mm:.0f} мм.")
            lines.append(f"Испарение: ~{p.etc_mm:.1f} мм/день "
                         f"(ET0 {p.et0_mm:.1f} × Kc {p.kc:g}).")
            lines.append(f"Источник Kc: {p.kc_source}.")

    rain = sum(x.rain_mm for x in rec.plan)
    if uz:
        lines.append(f"Yomg'ir (14 kun prognozi): ~{rain:.0f} mm.")
    else:
        lines.append(f"Дождь в прогнозе на 14 дней: ~{rain:.0f} мм.")

    if last_irr is not None:
        gap = (rec.generated_on - last_irr).days
        lines.append(f"Oxirgi sug'orish: {last_irr.day:02d}.{last_irr.month:02d} "
                     f"({gap} kun oldin)." if uz else
                     f"Последний полив: {last_irr.day:02d}.{last_irr.month:02d} "
                     f"({gap} дн. назад).")
    else:
        lines.append("Oxirgi sug'orish sanasi noma'lum — hisob taxminiy."
                     if uz else
                     "Дата последнего полива неизвестна — расчёт приблизительный.")

    lines.append("")
    if rec.reason_key == "season_over":
        # Числа выше посчитаны по хвосту кривой Kc, то есть по стерне:
        # выводить из них что-либо нельзя, и «почему» обязан сказать
        # то же, что сказали совет и карточка, а не водный баланс.
        lines.append("Xulosa: mavsum tugadi — bu dala bo'yicha suv "
                     "hisoblanmaydi." if uz else
                     "Вывод: сезон культуры закончился — воду по этому "
                     "полю не считаем.")
    elif rec.reason_key == "harvest_hold":
        lines.append("Xulosa: terim davri — sug'orish ataylab to'xtatilgan."
                     if uz else
                     "Вывод: идёт съём урожая — полив остановлен сознательно.")
    elif rec.action_day is None and last_irr is None:
        # Тот же отказ, что и в самом совете: «почва ещё влажная» —
        # это вывод из дефицита, отсчёт которого начат сегодня, потому
        # что другой точки отсчёта нет. Числа выше остаются — они
        # посчитаны честно; вывода из них не делаем.
        lines.append("Xulosa: javob yo'q — hisobni qaysi kundan "
                     "yuritishni bilmayman." if uz else
                     "Вывод: ответа нет — неизвестно, с какого дня "
                     "вести счёт.")
    elif rec.action_day is None:
        lines.append(("Xulosa: " if uz else "Вывод: ") +
                     (REASON_UZ if uz else REASON_RU).get(rec.reason_key, ""))
    else:
        when = (f"{rec.action_day.day:02d}.{rec.action_day.month:02d}")
        lines.append(f"Xulosa: zaxira chegaraga {when} kuni yetadi — "
                     f"sug'orish o'sha kunga belgilandi." if uz else
                     f"Вывод: запас дойдёт до порога {when} — "
                     f"полив назначен на этот день.")

    if degraded:
        lines.append("Diqqat: ob-havo xizmati javob bermadi, hisob "
                     "me'yorlar bo'yicha." if uz else
                     "Внимание: прогноз погоды не пришёл, расчёт по нормам.")
    return "\n".join(lines)


def savings_text(summary, lang: str = "uz") -> str:
    if not getattr(summary, "has_baseline", True):
        # Без прежнего расхода экономию не с чем сравнивать. «0 м³»
        # здесь — не правда, а отсутствие данных, и говорить надо это.
        if lang == "uz":
            return ("Tejamkorlikni hisoblash uchun avvalgi sarf kerak.\n"
                    "Bir sug'orishda qancha suv ketishini ayting.")
        # Русскую ветку на показе читает наблюдатель, а не разработчик:
        # «впишите baseline_m3_per_ha в конфиг поля» отправляло его в
        # файл, которого у него нет, и выдавало имя переменной за ответ.
        # Не хватает ровно одного — объёма одного полива, и знает его
        # фермер; узбекская ветка так и спрашивает, русская теперь
        # спрашивает о том же. Числа как не было, так и нет.
        return ("Экономию посчитать не с чем: прежний расход не задан.\n"
                "Спросите у фермера, сколько воды уходит за один полив.")
    # Дни, когда журнал молчал дольше двух прежних интервалов, в счёт не
    # вошли — и фермер должен это видеть, иначе цифра читается как «за
    # весь сезон», а она за отмеченные отрезки.
    silent = getattr(summary, "silent_days", 0) or 0
    # «Сэкономлено −124 м³» — не экономия и не по-русски. Минус значит,
    # что по совету ушло БОЛЬШЕ прежней привычки: исход законный
    # (растянутые интервалы дают больший разовый объём), прятать его
    # нельзя — но и называть экономией тоже.
    overrun = summary.saved_m3 < 0
    if lang == "uz":
        v = "Tasdiqlangan" if summary.verified else "Fermer ma'lumoti"
        text = ((f"Maslahat bo'yicha avvalgi odatdan "
                 f"{_num(abs(summary.saved_m3))} m³ ko'proq ketdi.\n"
                 f"Manba: {v}.") if overrun else
                (f"Mavsum boshidan: {_num(summary.saved_m3)} m³ suv tejaldi.\n"
                 f"Manba: {v}."))
        if silent:
            text += (f"\n{silent} kun belgisiz qoldi — hisobga kirmadi. "
                     "Har sug'orishdan keyin «✅ Suv berdim» bosing.")
        return text
    v = "Подтверждено счётчиком" if summary.verified else "Со слов фермера"
    text = ((f"По совету ушло на {_num(abs(summary.saved_m3))} м³ больше "
             f"прежней привычки.\nИсточник: {v}.") if overrun else
            (f"С начала сезона сэкономлено {_num(summary.saved_m3)} м³.\n"
             f"Источник: {v}."))
    if silent:
        text += f"\n{silent} дн. без отметок в счёт не вошли."
    return text
