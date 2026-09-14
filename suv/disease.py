"""
Болезни сада по метеоданным: РИСК, не диагноз.

Эксперимент «Диагноз по дождю» (19.08.2026, сад FAR-OLMA) показал:
по часовому ряду той же Open-Meteo, что кормит полив, классические
модели дают устойчивый сигнал риска — но увидеть болезнь может только
глаз на листе. Поэтому модуль везде говорит «условия заражения
выполнились», а не «сад болен», и каждый экран заканчивается «проверьте
листья». Рецептура — агроному, как и в spray.py.

Три болезни, три уровня уверенности — и текст это различает:
* парша (Venturia inaequalis) — ревизия таблицы Миллса (MacHardy &
  Gadoury 1989); часы листовой влажности против средней температуры,
  цифры сверены по Purdue/Penn State 14.09.2026 (6 ч при 17-23°C,
  ~28 ч при 4°C). Порог «сильное» — ×2 к базовому: соотношение
  light/heavy исходной таблицы Миллса (9→18 ч в оптимуме);
* бакожог (Erwinia amylovora, карантинный: в Узбекистане впервые найден
  в 2013, на яблоне в Самарканде) — правило BHWT из MARYBLYT, сверено
  дословно: цветок открыт + накоплено 110 градусо-часов выше 18,3°C +
  влага (≥0,25 мм в день или ≥2,5 мм накануне) + среднесуточная ≥15,6°C.
  Полный MARYBLYT ведёт EIP со сбросами; здесь базовое правило — оно
  консервативнее, в сторону предупреждения;
* мучнистая роса — ТОЛЬКО фон без статуса: источники о порогах спорят
  (Penn State: RH>70% при 18-27°C; Ohio State: RH>90% при 10-25°C),
  и рисовать статус по неконсенсусному порогу запрещает та же честность,
  что не даёт рисовать карту без замера (§1.1).

Листовая влажность — прокси: час с осадками или RH≥90% (роса/туман).
Датчика влажности листа в саду нет, и текст обязан говорить
«по метеомодели», а не изображать измерение.

Ядро чистое: ноль зависимостей от telegram, как field_status.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .field_status import Action, Section, Status
from .weather import HourlyWeather

# Пока только яблоня: таблица Миллса — про Venturia inaequalis, и
# эксперимент прогонялся на яблоневом саду. Груше её же таблицу
# приписывать нельзя без сверки (у груши свой возбудитель V. pirina).
DISEASE_CROPS = frozenset({"apple"})

# ------------------------------------------------------------- влажный лист
# Час «мокрый», если шёл дождь (порог мороси — тот же RAIN_EPS_MM, что
# в spray.py) или воздух у насыщения: RH≥90% — стандартный прокси
# росы/тумана там, где датчика листа нет.
WET_RAIN_MM = 0.2
WET_RH = 90.0
# Сухой разрыв короче этого не прерывает окно заражения — операционное
# правило таблиц Миллса (прерывистое увлажнение суммируется); часы при
# этом считаются только мокрые.
DRY_GAP_H = 8

# Глубина «состоявшихся» окон для секции карточки. Отчёт смотрит глубже.
SECTION_LOOKBACK_DAYS = 7
REPORT_PAST_DAYS = 14
REPORT_FORECAST_DAYS = 7

# Ревизия таблицы Миллса (MacHardy & Gadoury 1989): средняя температура
# мокрого периода °C -> часов влажности до заражения аскоспорами.
# Между точками — линейная интерполяция, вне диапазона таблицы порога
# нет и мы честно отказываемся оценивать (None).
MILLS: tuple[tuple[float, float], ...] = (
    (1.0, 40.5), (2.0, 34.7), (3.0, 29.6), (4.0, 27.8), (5.0, 21.2),
    (6.0, 18.0), (7.0, 15.4), (8.0, 13.4), (9.0, 12.2), (10.0, 11.0),
    (11.0, 9.0), (12.0, 8.3), (13.0, 8.0), (14.0, 7.0), (15.0, 7.0),
    (16.0, 6.1), (17.0, 6.0), (23.0, 6.0), (24.0, 6.1), (25.0, 8.0),
    (26.0, 11.3),
)
# «Сильное заражение» — вдвое больше часов, чем базовый порог:
# соотношение строк light/heavy исходной таблицы Миллса.
HEAVY_FACTOR = 2.0

# BHWT (MARYBLYT): пороги дословно из публикаций модели.
FB_DH_BASE_C = 18.3     # градусо-часы считаются выше этой температуры
FB_DH_NEED = 110.0      # накопить с открытия первых цветков
FB_WET_DAY_MM = 0.25    # влага в сам день…
FB_WET_PREV_MM = 2.5    # …или столько накануне
FB_TAVG_C = 15.6        # среднесуточная в день заражения

# Окно цветения яблони под Ташкентом — КАЛЕНДАРНОЕ ДОПУЩЕНИЕ, не
# наблюдение (реальную дату цветения продукт пока не спрашивает), и
# отчёт говорит об этом вслух. Распускание почек в ростере — 20.03.
BLOOM: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "apple": ((4, 1), (4, 30)),
}

# Фон мучнистой росы: день «благоприятный», если тепло, воздух влажный
# и дождя нет (дождь смывает споры — «болезнь сухой погоды»). Пороги —
# широкая сетка Penn State; источники расходятся, поэтому фон никогда
# не красит статус и в тексте помечен «ориентировочно».
MILDEW_RH_MIN = 70.0
MILDEW_T_MIN_C = 10.0
MILDEW_T_MAX_C = 25.0
MILDEW_WASH_MM = 2.0
MILDEW_WINDOW_DAYS = 7


@dataclass(frozen=True)
class WetWindow:
    """Слитый период листовой влажности: мокрые часы и их средняя
    температура. end — конец последнего мокрого часа."""

    start: datetime
    end: datetime
    wet_hours: int
    avg_temp: float


@dataclass(frozen=True)
class ScabCheck:
    """Один период влажности против таблицы Миллса."""

    window: WetWindow
    required_h: float | None      # None — температура вне таблицы
    infected: bool
    heavy: bool
    incubation: tuple[int, int] | None  # дней до видимых пятен


def _is_wet(h: HourlyWeather) -> bool:
    return h.rain_mm > WET_RAIN_MM or h.rh >= WET_RH


def wet_windows(hours: list[HourlyWeather]) -> list[WetWindow]:
    """Слить мокрые часы в окна; сухой разрыв короче DRY_GAP_H окно не
    прерывает, но в счёт часов не идёт."""
    runs: list[list[HourlyWeather]] = []
    for h in hours:
        if not _is_wet(h):
            continue
        if runs and (h.time - runs[-1][-1].time) < timedelta(hours=DRY_GAP_H):
            runs[-1].append(h)
        else:
            runs.append([h])
    out = []
    for run in runs:
        temps = [h.temp for h in run]
        out.append(WetWindow(
            start=run[0].time,
            end=run[-1].time + timedelta(hours=1),
            wet_hours=len(run),
            avg_temp=sum(temps) / len(temps)))
    return out


def mills_hours(temp_c: float) -> float | None:
    """Порог часов влажности по ревизии Миллса; None вне таблицы —
    гадать за краем данных нельзя."""
    if temp_c < MILLS[0][0] or temp_c > MILLS[-1][0]:
        return None
    for (t0, h0), (t1, h1) in zip(MILLS, MILLS[1:]):
        if t0 <= temp_c <= t1:
            if t1 == t0:
                return h0
            return h0 + (h1 - h0) * (temp_c - t0) / (t1 - t0)
    return None


def incubation_days(temp_c: float) -> tuple[int, int] | None:
    """Дней от заражения до видимых пятен — инкубационная колонка той же
    таблицы. Вне заполненных строк — None: обещать дату без опоры нельзя."""
    if 16.0 <= temp_c <= 23.0:
        return (9, 10)
    if 14.0 <= temp_c < 16.0:
        return (12, 13)
    if 12.0 <= temp_c < 14.0:
        return (14, 14)
    if 11.0 <= temp_c < 12.0:
        return (15, 15)
    if 10.0 <= temp_c < 11.0:
        return (16, 16)
    if 6.0 <= temp_c < 10.0:
        return (17, 17)
    return None


def scab_checks(hours: list[HourlyWeather]) -> list[ScabCheck]:
    out = []
    for w in wet_windows(hours):
        req = mills_hours(w.avg_temp)
        infected = req is not None and w.wet_hours >= req
        heavy = req is not None and w.wet_hours >= req * HEAVY_FACTOR
        out.append(ScabCheck(
            window=w, required_h=req, infected=infected, heavy=heavy,
            incubation=incubation_days(w.avg_temp) if infected else None))
    return out


def fire_blight_days(hours: list[HourlyWeather], bloom_start: date,
                     bloom_end: date) -> tuple[list[date], bool]:
    """Дни риска бакожога по BHWT + флаг «оценка неполная».

    Неполная — когда ряд не покрывает начало цветения: градусо-часы
    тогда занижены, и молчание модели ещё не значит «риска не было».
    """
    by_day: dict[date, list[HourlyWeather]] = {}
    for h in hours:
        by_day.setdefault(h.time.date(), []).append(h)
    if not by_day:
        return [], True
    partial = min(by_day) > bloom_start

    dh = 0.0
    risky: list[date] = []
    prev_rain = 0.0
    for day in sorted(by_day):
        rows = by_day[day]
        rain = sum(h.rain_mm for h in rows)
        if day >= bloom_start:
            dh += sum(max(0.0, h.temp - FB_DH_BASE_C) for h in rows)
        if bloom_start <= day <= bloom_end:
            t_avg = sum(h.temp for h in rows) / len(rows)
            wet = rain >= FB_WET_DAY_MM or prev_rain >= FB_WET_PREV_MM
            if dh >= FB_DH_NEED and wet and t_avg >= FB_TAVG_C:
                risky.append(day)
        prev_rain = rain
    return risky, partial


def mildew_background(hours: list[HourlyWeather], now: datetime,
                      days: int = MILDEW_WINDOW_DAYS) -> tuple[int, int]:
    """(благоприятных дней, всего посчитанных) за последние `days`
    полных суток. Только фон: статуса из этого не делается."""
    by_day: dict[date, list[HourlyWeather]] = {}
    since = now.date() - timedelta(days=days)
    for h in hours:
        d = h.time.date()
        if since <= d < now.date():
            by_day.setdefault(d, []).append(h)
    favorable = 0
    for rows in by_day.values():
        t_avg = sum(h.temp for h in rows) / len(rows)
        rh_avg = sum(h.rh for h in rows) / len(rows)
        rain = sum(h.rain_mm for h in rows)
        if (MILDEW_T_MIN_C <= t_avg <= MILDEW_T_MAX_C
                and rh_avg >= MILDEW_RH_MIN and rain < MILDEW_WASH_MM):
            favorable += 1
    return favorable, len(by_day)


# ---------------------------------------------------------------- вёрстка

REPORT_ACTION_UZ = "🦠 Kasallik hisoboti"
REPORT_ACTION_RU = "🦠 Отчёт о болезнях"


def _dm(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}"


def _window_stamp(w: WetWindow) -> str:
    """«05.09» или «05–06.09»: конец берём по последнему мокрому часу."""
    last = (w.end - timedelta(hours=1)).date()
    if w.start.date() == last:
        return _dm(w.start.date())
    if w.start.month == last.month:
        return f"{w.start.day:02d}–{_dm(last)}"
    return f"{_dm(w.start.date())}–{_dm(last)}"


def _split(checks: list[ScabCheck], now: datetime
           ) -> tuple[list[ScabCheck], list[ScabCheck]]:
    """Состоявшиеся против прогнозных: окно, не закрывшееся к `now`,
    целиком уходит в прогнозные — обещанного дождя ещё может не быть."""
    done = [c for c in checks if c.window.end <= now]
    ahead = [c for c in checks if c.window.end > now]
    return done, ahead


def build_section(hours: list[HourlyWeather] | None, crop_key: str,
                  now: datetime, lang: str = "uz") -> Section | None:
    """Секция «Болезни» для экрана Dala holati.

    None — культура не покрыта или ряд не пришёл: правило экрана —
    секция либо говорит правду, либо не выходит на сцену (как spray).
    Статус красят только состоявшиеся окна и прогноз ПАРШИ: у неё
    сверенная таблица. Бакожог и роса живут в полном отчёте.
    """
    if crop_key not in DISEASE_CROPS or not hours:
        return None
    uz = lang == "uz"
    title = "🦠 Kasalliklar" if uz else "🦠 Болезни"
    action = Action(REPORT_ACTION_UZ if uz else REPORT_ACTION_RU, "fs:kasal")

    done, ahead = _split(scab_checks(hours), now)
    horizon = now - timedelta(days=SECTION_LOOKBACK_DAYS)
    done = [c for c in done if c.infected and c.window.end >= horizon]
    ahead = [c for c in ahead if c.infected]

    if done:
        c = done[-1]
        heavy = any(x.heavy for x in done)
        status = Status.ALERT if heavy else Status.WARN
        stamp = _window_stamp(c.window)
        deg = f"~{c.window.avg_temp:.0f}°"
        if uz:
            line = (f"Parsha: {stamp} — {c.window.wet_hours} soat namlik, "
                    f"{deg} — yuqish sharti bajarildi"
                    + (" (kuchli)" if heavy else ""))
        else:
            line = (f"Парша: {stamp} — {c.window.wet_hours} ч влажности, "
                    f"{deg} — условия заражения выполнились"
                    + (" (сильное)" if heavy else ""))
        # Инкубация — самое действенное, что можно сказать: когда именно
        # смотреть листья. Без строки таблицы дату не обещаем.
        if c.incubation:
            lo = (c.window.end + timedelta(days=c.incubation[0])).date()
            hi = (c.window.end + timedelta(days=c.incubation[1])).date()
            when = _dm(lo) if lo == hi else f"{_dm(lo)}–{_dm(hi)}"
            hint = (f"Meteo-model bo'yicha · dog'lar ~{when} — "
                    f"barglarni tekshiring" if uz else
                    f"По метеомодели · пятна ~{when} — проверьте листья")
        else:
            hint = ("Meteo-model bo'yicha — barglarni tekshiring" if uz
                    else "По метеомодели — проверьте листья")
        return Section(key="disease", order=33, title=title, status=status,
                       line=line, hint=hint, action=action)

    if ahead:
        c = ahead[0]
        stamp = _window_stamp(c.window)
        deg = f"~{c.window.avg_temp:.0f}°"
        if uz:
            line = (f"Parsha: {stamp} ~{c.window.wet_hours} soat namlik "
                    f"kutilmoqda ({deg}) — prognoz bo'yicha")
            hint = "Yuqish sharti bajarilishi mumkin — purkash oynasiga qarang"
        else:
            line = (f"Парша: {stamp} ожидается ~{c.window.wet_hours} ч "
                    f"влажности ({deg}) — по прогнозу")
            hint = "Возможно заражение — смотрите окно опрыскивания"
        return Section(key="disease", order=33, title=title,
                       status=Status.WARN, line=line, hint=hint,
                       action=action)

    line = (f"So'nggi {SECTION_LOOKBACK_DAYS} kunda parsha sharti "
            "bajarilmadi" if uz else
            f"За {SECTION_LOOKBACK_DAYS} дней условий заражения "
            "паршой не было")
    return Section(key="disease", order=33, title=title, status=Status.OK,
                   line=line,
                   hint=("Meteo-model bo'yicha" if uz else "По метеомодели"),
                   action=action)


# Показывать в отчёте не больше стольких прошедших окон: у Telegram
# потолок 4096 символов, а старое окно двухнедельной давности читателю
# менее ценно, чем свежие.
REPORT_MAX_DONE = 6
REPORT_MAX_AHEAD = 3


def _check_line(c: ScabCheck, uz: bool) -> str:
    w = c.window
    stamp = _window_stamp(w)
    deg = f"~{w.avg_temp:.0f}°"
    if c.required_h is None:
        verdict = ("harorat jadvaldan tashqari — baholamayman" if uz
                   else "температура вне таблицы — не оцениваю")
    elif c.heavy:
        verdict = "KUCHLI yuqish sharti" if uz else "условия СИЛЬНОГО заражения"
    elif c.infected:
        verdict = ("yuqish sharti bajarildi" if uz
                   else "условия заражения выполнились")
    else:
        need = f"{c.required_h:.0f}"
        verdict = (f"xavfsiz (kerak {need} soat)" if uz
                   else f"безопасно (нужно {need} ч)")
    unit = "soat nam" if uz else "ч влажности"
    return f"• {stamp} · {w.wet_hours} {unit} · {deg} → {verdict}"


def build_report(hours: list[HourlyWeather], crop_key: str, now: datetime,
                 lang: str = "uz", crop_name: str | None = None) -> str:
    """Полный отчёт о болезнях — drill-down из секции карточки.

    Максимум подробностей при том же градусе честности: каждое окно с
    числами, каждая модель названа, каждое допущение произнесено.
    """
    uz = lang == "uz"
    checks = scab_checks(hours)
    done, ahead = _split(checks, now)
    horizon = now - timedelta(days=REPORT_PAST_DAYS)
    done = [c for c in done if c.window.end >= horizon][-REPORT_MAX_DONE:]
    ahead = ahead[:REPORT_MAX_AHEAD]

    d0 = min((h.time for h in hours), default=now).date()
    d1 = max((h.time for h in hours), default=now).date()
    name = crop_name or crop_key
    parts: list[str] = []

    if uz:
        parts.append(f"🦠 {name} — kasallik hisoboti\n"
                     f"Davr: {_dm(d0)}–{_dm(d1)} · ob-havo modeli bo'yicha, "
                     "dala o'lchovi emas")
    else:
        parts.append(f"🦠 {name} — отчёт о болезнях\n"
                     f"Период: {_dm(d0)}–{_dm(d1)} · по метеомодели, "
                     "не полевой замер")

    # -------- парша
    scab = ["🍂 Parsha (qo'tir) — Mills jadvali (qayta ko'rilgan):" if uz
            else "🍂 Парша — таблица Миллса (ревизия):"]
    if not done and not ahead:
        scab.append("Namlik oynalari kuzatilmadi." if uz
                    else "Окон листовой влажности не было.")
    scab.extend(_check_line(c, uz) for c in done)

    infected = [c for c in done if c.infected]
    if infected:
        last = infected[-1]
        if last.incubation:
            lo = (last.window.end + timedelta(days=last.incubation[0])).date()
            hi = (last.window.end + timedelta(days=last.incubation[1])).date()
            when = _dm(lo) if lo == hi else f"{_dm(lo)}–{_dm(hi)}"
            scab.append(
                f"Oxirgi yuqish {_window_stamp(last.window)} — dog'lar "
                f"taxminan {when} ko'rinadi, barglarni tekshiring." if uz else
                f"Последнее заражение {_window_stamp(last.window)} — пятна "
                f"проявятся примерно {when}, проверьте листья.")
    for c in ahead:
        w = c.window
        scab.append(
            f"Prognozda {_window_stamp(w)}: ~{w.wet_hours} soat namlik "
            f"(~{w.avg_temp:.0f}°) — "
            + ("yuqish ehtimoli bor." if c.infected else "xavf past.")
            if uz else
            f"По прогнозу {_window_stamp(w)}: ~{w.wet_hours} ч влажности "
            f"(~{w.avg_temp:.0f}°) — "
            + ("возможно заражение." if c.infected else "риск низкий."))
    parts.append("\n".join(scab))

    # -------- бакожог
    year = now.year
    (bm, bd), (em, ed) = BLOOM[crop_key]
    bloom_start, bloom_end = date(year, bm, bd), date(year, em, ed)
    fb = ["🔥 Bakterial kuyish (karantin kasalligi):" if uz
          else "🔥 Бакожог (карантинная болезнь):"]
    if now.date() > bloom_end + timedelta(days=14):
        fb.append(
            "Gullash davri (aprel) o'tgan — bu kasallik asosan gullashda "
            "yuqadi. Qurigan, kuygansimon novda ko'rsangiz — agronomga va "
            "o'simliklar karantini xizmatiga ko'rsating." if uz else
            "Период цветения (апрель) прошёл — заражение идёт в основном "
            "через цветок. Увидите усохшие, будто обожжённые побеги — "
            "покажите агроному и карантинной службе.")
    elif now.date() < bloom_start:
        fb.append(
            f"Xavf gullashda ({_dm(bloom_start)}–{_dm(bloom_end)} deb "
            "olingan, kalendar bo'yicha) — o'shanda BHWT qoidasi bilan "
            "kunma-kun baholayman." if uz else
            f"Риск — в цветение ({_dm(bloom_start)}–{_dm(bloom_end)}, "
            "взято по календарю) — тогда оценю по дням правилом BHWT.")
    else:
        risky, partial = fire_blight_days(hours, bloom_start, bloom_end)
        if risky:
            days_s = ", ".join(_dm(d) for d in risky)
            fb.append(f"BHWT bo'yicha xavfli kunlar: {days_s}." if uz
                      else f"Дни риска по BHWT: {days_s}.")
        else:
            fb.append("BHWT sharti to'liq bajarilgan kun yo'q." if uz
                      else "Дней с полным условием BHWT не было.")
        if partial:
            fb.append(
                "Qator gullash boshini qamramaydi — gradus-soat yig'indisi "
                "to'liq emas, xavf kamaytirib baholangan bo'lishi mumkin."
                if uz else
                "Ряд не покрывает начало цветения — сумма градусо-часов "
                "неполна, риск мог быть недооценён.")
        fb.append(
            f"(Gul ochilishi {_dm(bloom_start)} deb olindi — kalendar "
            "bo'yicha, kuzatuv emas.)" if uz else
            f"(Начало цветения взято {_dm(bloom_start)} — по календарю, "
            "не по наблюдению.)")
    parts.append("\n".join(fb))

    # -------- мучнистая роса
    fav, total = mildew_background(hours, now)
    if total:
        parts.append(
            f"🌫 Un-shudring: so'nggi {total} kunning {fav} tasi qulay "
            "ob-havo (iliq, nam havo, yomg'irsiz). Bu faqat fon — aniq "
            "chegara manbalarda kelishilmagan." if uz else
            f"🌫 Мучнистая роса: {fav} из {total} последних дней — "
            "благоприятная погода (тепло, влажный воздух, без дождя). "
            "Это только фон — единого порога в источниках нет.")

    parts.append(
        "⚠️ Bu XAVF hisoboti, tashxis emas: kasallikni faqat barg va "
        "mevada ko'rish tasdiqlaydi. Dori tanlash — agronom ishi. Purkash "
        "uchun qulay oyna — «🌾 Dala holati» ekranida." if uz else
        "⚠️ Это отчёт о РИСКЕ, не диагноз: подтвердить болезнь может "
        "только осмотр листьев и плодов. Выбор препарата — дело агронома. "
        "Окно для опрыскивания — на экране «🌾 Dala holati».")

    return "\n\n".join(parts)
