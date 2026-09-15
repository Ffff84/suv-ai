"""
Болезни сада по метеоданным: РИСК, не диагноз.

Эксперимент «Диагноз по дождю» (19.08.2026, сад FAR-OLMA) показал:
по часовому ряду той же Open-Meteo, что кормит полив, классические
модели дают устойчивый сигнал риска — но увидеть болезнь может только
глаз на листе. Поэтому модуль везде говорит «условия заражения
выполнились», а не «сад болен», и каждый экран заканчивается «проверьте
листья». Рецептура — агроному, как и в spray.py.

С 15.09.2026 покрытие расширено на 14 культур движка — реестр, планка
доказательности и источники описаны у STATUS_CROPS/BG_ONLY_CROPS ниже.
Исходный яблоневый блок — три болезни, три уровня уверенности, и текст
это различает:
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

# Расширение 15.09.2026: покрытие по культурам движка — та же планка,
# что у яблони. СТАТУС красят только модели, чьи пороги сверены с
# первоисточником дословно; спорные пороги — ФОН (informational, статус
# поля не трогают); культуры без погодной модели с консенсусом честно
# не покрыты вовсе (раздел не выходит на сцену).
#
# Сверено 15.09.2026:
# * фитофтороз (картофель, томат) — критерии Хаттона (James Hutton
#   Institute / AHDB, 2017, сменили Smith Periods): два дня подряд с
#   min T >= 10°C и >= 6 ч RH >= 90% в каждый. Возбудитель на томате
#   тот же P. infestans; что модель картофельная — говорим вслух.
# * милдью винограда — правило «10-10-10» (Baldacci, 1947; итальянские
#   системы предупреждений): T >= 10°C, дождь >= 10 мм за 24–48 ч,
#   побег >= 10 см. Правило перестраховывается (даёт риск и там, где
#   заражения не было) — в сторону предупреждения, как наш BHWT.
#   Длину побега продукт не знает — окно сезона взято календарём,
#   и текст это произносит.
# * оидиум винограда — ядро индекса Gubler–Thomas (UC Davis):
#   >= 6 непрерывных часов при 21,1–29,4°C (70–85°F) за день -> +20,
#   иначе -10; индекс 0–100. Фазу аскоспор и жаро-сброс (>= 35°C) не
#   реализуем — без них индекс выше, то есть консервативнее к риску.
#   Категории источника: 0–30 низкий, 40–50 средний, 60–100 высокий;
#   зазоры отнесены вниз (30–39 — низкий, 50–59 — средний).
#
# Только фон (пороги в источниках расходятся или таблицу сверить не
# удалось — статус не красим, п. §1.1):
# * монилиоз цветения косточковых (при 20–25° хватает 3–5 ч влаги, но
#   единой операционной таблицы нет), * альтернариоз томата (таблица
#   TOMCAST не сверена по первоисточнику), * фузариоз колоса пшеницы и
#   ячменя, * пероноспороз лука (DOWNCAST не сверен), * пероноспороз
#   тыквенных (главный фактор — прилёт спор, локальная погода его не
#   видит), * курчавость персика (окно до распускания почек).
#
# Груша НЕ добавлена: у неё свой возбудитель парши (V. pirina),
# таблицу Миллса приписывать нельзя без сверки.
STATUS_CROPS = frozenset({
    "apple", "grape", "tomato", "potato", "potato_summer",
})
BG_ONLY_CROPS = frozenset({
    "peach", "apricot", "cherry", "winter_wheat", "barley",
    "onion", "cucumber", "melon", "watermelon",
})
DISEASE_CROPS = STATUS_CROPS | BG_ONLY_CROPS

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
    # Косточковые: окна цветения под Самаркандом — календарные допущения,
    # как у яблони; отчёт произносит это явно.
    "apricot": ((3, 20), (4, 10)),
    "peach": ((3, 25), (4, 15)),
    "cherry": ((4, 1), (4, 20)),
    # Зерновые: «цветение» здесь — колошение-цветение, окно фузариоза.
    "winter_wheat": ((5, 1), (5, 25)),
    "barley": ((4, 20), (5, 15)),
}

# Правило 10-10-10: окно сезона, в котором побег винограда считается
# длиннее 10 см, — календарное допущение вместо фенологии.
GRAPE_SEASON = ((4, 20), (7, 31))

# Критерии Хаттона — дословно (James Hutton Institute / AHDB, 2017).
HUTTON_TMIN_C = 10.0
HUTTON_RH = 90.0
HUTTON_RH_HOURS = 6

# Правило 10-10-10 — дословно (Baldacci, 1947).
R10_TEMP_C = 10.0
R10_RAIN_MM = 10.0

# Ядро индекса Gubler–Thomas: 70–85°F в градусах Цельсия.
GT_T_LOW_C = 21.1
GT_T_HIGH_C = 29.4
GT_RUN_HOURS = 6
GT_ADD = 20
GT_SUB = 10
GT_HIGH = 60
GT_MODERATE = 40

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


# ------------------------------------------------- модели других культур

def _by_day(hours: list[HourlyWeather]) -> dict[date, list[HourlyWeather]]:
    out: dict[date, list[HourlyWeather]] = {}
    for h in hours:
        out.setdefault(h.time.date(), []).append(h)
    return out


def hutton_days(hours: list[HourlyWeather]) -> list[date]:
    """Дни, замыкающие пару суток по критериям Хаттона: в оба дня
    min T >= 10°C и >= 6 часов RH >= 90%. Дословно по публикации
    (заменили Smith Periods в 2017). Возвращается второй день пары."""
    by_day = _by_day(hours)
    def ok(rows: list[HourlyWeather]) -> bool:
        return (min(h.temp for h in rows) >= HUTTON_TMIN_C
                and sum(1 for h in rows if h.rh >= HUTTON_RH)
                >= HUTTON_RH_HOURS)
    days = sorted(by_day)
    out = []
    for prev, cur in zip(days, days[1:]):
        if ((cur - prev).days == 1 and len(by_day[prev]) >= 20
                and len(by_day[cur]) >= 20
                and ok(by_day[prev]) and ok(by_day[cur])):
            out.append(cur)
    return out


def rule_10_10_10_days(hours: list[HourlyWeather], year: int) -> list[date]:
    """Дни первичного заражения милдью по правилу 10-10-10: за текущие
    и предыдущие сутки вместе >= 10 мм дождя, среднесуточная T обоих
    дней >= 10°C, внутри календарного окна «побег длиннее 10 см»."""
    (sm, sd), (em, ed) = GRAPE_SEASON
    lo, hi = date(year, sm, sd), date(year, em, ed)
    by_day = _by_day(hours)
    days = sorted(by_day)
    out = []
    for prev, cur in zip(days, days[1:]):
        if not (lo <= cur <= hi and (cur - prev).days == 1):
            continue
        rain2 = (sum(h.rain_mm for h in by_day[prev])
                 + sum(h.rain_mm for h in by_day[cur]))
        t_ok = all(
            sum(h.temp for h in by_day[d]) / len(by_day[d]) >= R10_TEMP_C
            for d in (prev, cur))
        if rain2 >= R10_RAIN_MM and t_ok:
            out.append(cur)
    return out


def gt_index(hours: list[HourlyWeather], now: datetime) -> tuple[int, int]:
    """(индекс 0–100, дней в расчёте) по ядру Gubler–Thomas: день с
    >= 6 непрерывными часами при 21,1–29,4°C даёт +20, иначе -10.
    Индекс стартует с нуля в начале доступного ряда (обычно 14 дней) —
    в начале ряда он занижен, и текст отчёта говорит это вслух."""
    by_day = _by_day(hours)
    idx = 0
    n = 0
    for day in sorted(by_day):
        if day >= now.date():
            break
        rows = sorted(by_day[day], key=lambda h: h.time)
        if len(rows) < 20:
            continue
        run = best = 0
        for h in rows:
            run = run + 1 if GT_T_LOW_C <= h.temp <= GT_T_HIGH_C else 0
            best = max(best, run)
        idx = min(100, max(0, idx + (GT_ADD if best >= GT_RUN_HOURS
                                     else -GT_SUB)))
        n += 1
    return idx, n


def _wet_hours_between(hours: list[HourlyWeather], lo: date, hi: date) -> int:
    return sum(1 for h in hours if lo <= h.time.date() <= hi and _is_wet(h))


def humid_days(hours: list[HourlyWeather], lo: date, hi: date,
               rh: float = 90.0, need_h: int = 6) -> tuple[int, int]:
    """(влажных дней, всего дней ряда) в окне: день влажный, если в нём
    >= need_h часов с RH >= rh. Для фоновых блоков."""
    by_day = _by_day(hours)
    wet = total = 0
    for day, rows in by_day.items():
        if lo <= day <= hi:
            total += 1
            if sum(1 for h in rows if h.rh >= rh) >= need_h:
                wet += 1
    return wet, total


def wet_nights(hours: list[HourlyWeather], now: datetime,
               days: int = 7, rh: float = 95.0, need_h: int = 3
               ) -> tuple[int, int]:
    """(влажных ночей, всего ночей) за последние `days` суток: ночь
    влажная, если с 0:00 до 6:59 набралось >= need_h часов RH >= rh.
    Фон пероноспороза; полную модель (DOWNCAST) не сверяли."""
    since = now.date() - timedelta(days=days)
    by_day: dict[date, int] = {}
    seen: set[date] = set()
    for h in hours:
        d = h.time.date()
        if since <= d < now.date() and h.time.hour < 7:
            seen.add(d)
            if h.rh >= rh:
                by_day[d] = by_day.get(d, 0) + 1
    return sum(1 for v in by_day.values() if v >= need_h), len(seen)


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


def _section_hutton(hours: list[HourlyWeather], now: datetime, uz: bool,
                    title: str, action: Action) -> Section:
    """Картофель и томат: фитофтороз по критериям Хаттона."""
    days = hutton_days(hours)
    horizon = now.date() - timedelta(days=SECTION_LOOKBACK_DAYS)
    done = [d for d in days if horizon <= d < now.date()]
    ahead = [d for d in days if d >= now.date()]
    # Карточка говорит языком фермера: что за погода была и куда
    # смотреть глазами. Имя модели живёт в полном отчёте.
    if done:
        stamp = ", ".join(_dm(d) for d in done[-3:])
        status = Status.ALERT if len(done) >= 2 else Status.WARN
        line = (f"Fitoftoroz: {stamp} — kasallik uchun qulay nam ob-havo "
                "bo'ldi" if uz else
                f"Фитофтороз: {stamp} — погода была благоприятной для "
                "болезни")
        hint = ("Barg va poyada qo'ng'ir dog' bormi — tekshiring" if uz
                else "Проверьте бурые пятна на листьях и стеблях")
    elif ahead:
        status = Status.WARN
        line = (f"Fitoftoroz: {_dm(ahead[0])} kuni qulay nam ob-havo "
                "kutilmoqda" if uz else
                f"Фитофтороз: {_dm(ahead[0])} ожидается благоприятная "
                "для болезни погода")
        hint = ("Hozircha prognoz — dalani kuzating" if uz
                else "Пока это прогноз — наблюдайте за полем")
    else:
        status = Status.OK
        line = (f"So'nggi {SECTION_LOOKBACK_DAYS} kunda fitoftoroz uchun "
                "qulay ob-havo bo'lmadi" if uz else
                f"За {SECTION_LOOKBACK_DAYS} дней погоды, благоприятной "
                "для фитофтороза, не было")
        hint = "Meteo-model bo'yicha" if uz else "По метеомодели"
    return Section(key="disease", order=33, title=title, status=status,
                   line=line, hint=hint, action=action)


def _section_grape(hours: list[HourlyWeather], now: datetime, uz: bool,
                   title: str, action: Action) -> Section:
    """Виноград: милдью по 10-10-10, оидиум — ядро индекса GT."""
    events = rule_10_10_10_days(hours, now.year)
    horizon = now.date() - timedelta(days=SECTION_LOOKBACK_DAYS)
    done = [d for d in events if horizon <= d < now.date()]
    ahead = [d for d in events if d >= now.date()]
    idx, _n = gt_index(hours, now)
    # Число индекса — в отчёте; карточка говорит словом: bosim
    # past/o'rtacha/yuqori — как погода, а не как приборная шкала.
    cat = (("yuqori" if uz else "высокое") if idx >= GT_HIGH else
           ("o'rtacha" if uz else "среднее") if idx >= GT_MODERATE else
           ("past" if uz else "низкое"))
    if done:
        stamp = ", ".join(_dm(d) for d in done[-3:])
        status = Status.ALERT if idx >= GT_HIGH else Status.WARN
        line = (f"Mildyu: {stamp} — issiq yomg'ir o'tdi, yuqish mumkin"
                if uz else
                f"Милдью: {stamp} — прошёл тёплый дождь, возможно "
                "заражение")
        hint = ("Barg ostida oq g'ubor bormi — tekshiring" if uz
                else "Проверьте белый налёт с нижней стороны листа")
        if idx >= GT_HIGH:
            hint = ("Barg ostida oq g'ubor va kulrang un kabi g'uborni "
                    "tekshiring" if uz else
                    "Проверьте белый налёт снизу листа и серый мучнистый "
                    "налёт")
    elif ahead:
        status = Status.WARN
        line = (f"Mildyu: {_dm(ahead[0])} kuni issiq yomg'ir kutilmoqda"
                if uz else
                f"Милдью: {_dm(ahead[0])} ожидается тёплый дождь")
        hint = ("Yomg'irdan keyin barg ostini tekshiring" if uz
                else "После дождя проверьте низ листьев")
    elif idx >= GT_HIGH:
        status = Status.WARN
        line = ("Oidium: issiq kunlar ko'p — kasallik bosimi yuqori" if uz
                else "Оидиум: жарких дней много — давление болезни "
                "высокое")
        hint = ("Barg va g'ujumda kulrang-oq g'uborni tekshiring" if uz
                else "Проверьте серо-белый налёт на листьях и гроздях")
    else:
        status = Status.OK
        line = (f"Kasallik uchun qulay ob-havo bo'lmadi · oidium bosimi "
                f"{cat}" if uz else
                f"Погоды, благоприятной для болезней, не было · давление "
                f"оидиума {cat}")
        hint = "Meteo-model bo'yicha" if uz else "По метеомодели"
    return Section(key="disease", order=33, title=title, status=status,
                   line=line, hint=hint, action=action)


def _section_bg(hours: list[HourlyWeather], crop_key: str, now: datetime,
                uz: bool, title: str, action: Action) -> Section:
    """Культуры, где есть только фон: секция информационная, статус
    поля не трогает (Status.NO_DATA + informational)."""
    year = now.year
    hint = ("Fon, tashxis emas — batafsil hisobotda" if uz
            else "Фон, не статус — подробности в отчёте")
    if crop_key in ("peach", "apricot", "cherry"):
        (bm, bd), (em, ed) = BLOOM[crop_key]
        lo, hi = date(year, bm, bd), date(year, em, ed)
        if lo - timedelta(days=3) <= now.date() <= hi + timedelta(days=3):
            wet_h = _wet_hours_between(hours, lo, min(hi, now.date()))
            line = (f"Gullashda {wet_h} soat namlik — monilioz uchun fon"
                    if uz else
                    f"Во время цветения {wet_h} ч влажности — фон монилиоза")
        else:
            line = (f"Monilioz xavfi — gullashda ({_dm(lo)}–{_dm(hi)}, "
                    "kalendar bo'yicha)" if uz else
                    f"Риск монилиоза — в цветение ({_dm(lo)}–{_dm(hi)}, "
                    "по календарю)")
    elif crop_key in ("winter_wheat", "barley"):
        (bm, bd), (em, ed) = BLOOM[crop_key]
        lo, hi = date(year, bm, bd), date(year, em, ed)
        if lo <= now.date() <= hi + timedelta(days=7):
            wet, total = humid_days(hours, lo, min(hi, now.date()))
            line = (f"Boshoqlashda nam kunlar: {wet}/{total} — fuzarioz "
                    "foni" if uz else
                    f"Влажных дней в колошение: {wet}/{total} — фон "
                    "фузариоза")
        else:
            line = (f"Fuzarioz xavfi — boshoqlash-gullashda ({_dm(lo)}–"
                    f"{_dm(hi)}, kalendar)" if uz else
                    f"Риск фузариоза — в колошение-цветение ({_dm(lo)}–"
                    f"{_dm(hi)}, по календарю)")
    else:  # onion, cucumber, melon, watermelon
        wet, total = wet_nights(hours, now)
        line = (f"Nam tunlar: {wet}/{total} — peronosporoz foni" if uz
                else f"Влажных ночей: {wet}/{total} — фон пероноспороза")
        if crop_key != "onion":
            hint = ("Asosiy omil — spora kelishi; ob-havo uni ko'rmaydi"
                    if uz else
                    "Главный фактор — прилёт спор; погода его не видит")
    return Section(key="disease", order=33, title=title,
                   status=Status.NO_DATA, line=line, hint=hint,
                   action=action, informational=True)


def build_section(hours: list[HourlyWeather] | None, crop_key: str,
                  now: datetime, lang: str = "uz") -> Section | None:
    """Секция «Болезни» для экрана Dala holati.

    None — культура не покрыта или ряд не пришёл: правило экрана —
    секция либо говорит правду, либо не выходит на сцену (как spray).
    Статус красят только сверенные модели: парша (Миллс) у яблони,
    Хаттон у картофеля и томата, 10-10-10 и индекс GT у винограда.
    Фоновые культуры — информационная секция без статуса.
    """
    if crop_key not in DISEASE_CROPS or not hours:
        return None
    uz = lang == "uz"
    title = "🦠 Kasalliklar" if uz else "🦠 Болезни"
    action = Action(REPORT_ACTION_UZ if uz else REPORT_ACTION_RU, "fs:kasal")
    if crop_key in ("potato", "potato_summer", "tomato"):
        return _section_hutton(hours, now, uz, title, action)
    if crop_key == "grape":
        return _section_grape(hours, now, uz, title, action)
    if crop_key in BG_ONLY_CROPS:
        return _section_bg(hours, crop_key, now, uz, title, action)

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
    if crop_key != "apple":
        return _report_other(hours, crop_key, now, uz, crop_name)
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

    parts.append(_final_disclaimer(uz))

    return "\n\n".join(parts)


def _final_disclaimer(uz: bool) -> str:
    return (
        "⚠️ Bu XAVF hisoboti, tashxis emas: kasallikni faqat barg va "
        "mevada ko'rish tasdiqlaydi. Dori tanlash — agronom ishi. Purkash "
        "uchun qulay oyna — «🌾 Dala holati» ekranida." if uz else
        "⚠️ Это отчёт о РИСКЕ, не диагноз: подтвердить болезнь может "
        "только осмотр листьев и плодов. Выбор препарата — дело агронома. "
        "Окно для опрыскивания — на экране «🌾 Dala holati».")


def _report_header(hours: list[HourlyWeather], now: datetime, uz: bool,
                   name: str) -> str:
    d0 = min((h.time for h in hours), default=now).date()
    d1 = max((h.time for h in hours), default=now).date()
    if uz:
        return (f"🦠 {name} — kasallik hisoboti\n"
                f"Davr: {_dm(d0)}–{_dm(d1)} · ob-havo modeli bo'yicha, "
                "dala o'lchovi emas")
    return (f"🦠 {name} — отчёт о болезнях\n"
            f"Период: {_dm(d0)}–{_dm(d1)} · по метеомодели, "
            "не полевой замер")


def _report_other(hours: list[HourlyWeather], crop_key: str, now: datetime,
                  uz: bool, crop_name: str | None) -> str:
    """Полный отчёт для культур, добавленных 15.09.2026. Планка та же:
    каждая модель названа, каждый источник и каждое допущение —
    произнесены; несверенное не красится."""
    year = now.year
    parts = [_report_header(hours, now, uz, crop_name or crop_key)]

    if crop_key in ("potato", "potato_summer", "tomato"):
        days = hutton_days(hours)
        done = [d for d in days if d < now.date()]
        ahead = [d for d in days if d >= now.date()]
        blk = ["🥀 Fitoftoroz" if uz else "🥀 Фитофтороз"]
        if done:
            days_s = ", ".join(_dm(d) for d in done[-REPORT_MAX_DONE:])
            blk.append(
                f"Nima bo'ldi: {days_s} kunlari ikki kun ketma-ket iliq "
                "va juda nam bo'ldi — kasallik yuqishi uchun qulay "
                "ob-havo." if uz else
                f"Что случилось: {days_s} два дня подряд было тепло и "
                "очень влажно — благоприятная для заражения погода.")
        else:
            blk.append("Kasallik uchun qulay ob-havo kuzatilmadi." if uz
                       else "Погоды, благоприятной для болезни, не было.")
        for d in ahead[:REPORT_MAX_AHEAD]:
            blk.append(f"Prognoz: {_dm(d)} kuni ham shunday ob-havo "
                       "kutilmoqda." if uz else
                       f"Прогноз: {_dm(d)} ожидается такая же погода.")
        blk.append(
            "Nima qilish: barg va poyada qo'ng'ir dog'larni qidiring; "
            "nam kunda barg ostida oq g'ubor bo'lishi mumkin. Topsangiz "
            "— rasmga olib agronomga ko'rsating." if uz else
            "Что делать: ищите бурые пятна на листьях и стеблях; во "
            "влажный день снизу листа может быть белый налёт. Нашли — "
            "сфотографируйте и покажите агроному.")
        blk.append(
            "Model (agronom uchun): Hutton mezoni (2017): ikki kun "
            "ketma-ket min T≥10° va ≥6 soat RH≥90%." if uz else
            "Модель (для агронома): критерии Хаттона (2017): два дня "
            "подряд min T≥10° и ≥6 ч RH≥90%.")
        if crop_key == "tomato":
            blk.append(
                "Model kartoshka uchun tuzilgan; pomidorda qo'zg'atuvchi "
                "o'sha — P. infestans." if uz else
                "Модель разработана для картофеля; возбудитель на томате "
                "тот же — P. infestans.")
        parts.append("\n".join(blk))
        if crop_key == "tomato":
            parts.append(
                "🍂 Alternarioz: belgisi — pastki barglarda halqali "
                "qo'ng'ir dog'lar. Ob-havo bo'yicha faqat fon beramiz: "
                "ishonchli jadvalni (TOMCAST) birlamchi manba bilan "
                "solishtirolmadik, holat bo'yalmaydi." if uz else
                "🍂 Альтернариоз: признак — кольцевые бурые пятна на "
                "нижних листьях. По погоде даём только фон: таблицу "
                "TOMCAST не удалось сверить с первоисточником, статус "
                "не красим.")

    elif crop_key == "grape":
        events = rule_10_10_10_days(hours, year)
        done = [d for d in events if d < now.date()]
        ahead = [d for d in events if d >= now.date()]
        (sm, sd), (em, ed) = GRAPE_SEASON
        blk = ["🍇 Mildyu" if uz else "🍇 Милдью"]
        if done:
            days_s = ", ".join(_dm(d) for d in done[-REPORT_MAX_DONE:])
            blk.append(f"Nima bo'ldi: {days_s} kunlari issiq yomg'ir "
                       "o'tdi — kasallik aynan shunday kunlarda yuqadi."
                       if uz else
                       f"Что случилось: {days_s} прошёл тёплый дождь — "
                       "болезнь заражает именно в такие дни.")
        else:
            blk.append("Issiq yomg'irli kun bo'lmadi — yuqish sharoiti "
                       "kuzatilmadi." if uz else
                       "Тёплых дождливых дней не было — условий для "
                       "заражения не наблюдалось.")
        for d in ahead[:REPORT_MAX_AHEAD]:
            blk.append(f"Prognoz: {_dm(d)} kuni issiq yomg'ir kutilmoqda."
                       if uz else
                       f"Прогноз: {_dm(d)} ожидается тёплый дождь.")
        blk.append(
            "Nima qilish: barg ustida yog'li sariq dog', ostida oq "
            "g'ubor — shu belgilarni qidiring. Topsangiz — agronomga "
            "rasm ko'rsating." if uz else
            "Что делать: ищите маслянистые жёлтые пятна сверху листа и "
            "белый налёт снизу. Нашли — покажите фото агроному.")
        blk.append(
            f"Model (agronom uchun): «10-10-10» qoidasi (Baldacci, "
            f"1947): 24–48 soatda ≥10 mm yomg'ir, T≥10°, novda ≥10 sm "
            f"(kalendar: {sd:02d}.{sm:02d}–{ed:02d}.{em:02d}). Qoida "
            "ehtiyotkor — ortiqcha ogohlantirishi mumkin." if uz else
            f"Модель (для агронома): правило «10-10-10» (Baldacci, "
            f"1947): ≥10 мм дождя за 24–48 ч при T≥10°, побег ≥10 см "
            f"(календарь: {sd:02d}.{sm:02d}–{ed:02d}.{em:02d}). Правило "
            "перестраховывается — может предупредить без заражения.")
        parts.append("\n".join(blk))

        idx, n = gt_index(hours, now)
        cat = (("yuqori" if uz else "высокое") if idx >= GT_HIGH else
               ("o'rtacha" if uz else "среднее") if idx >= GT_MODERATE
               else ("past" if uz else "низкое"))
        parts.append(
            f"🌫 Oidium\n"
            f"Kasallik bosimi: {cat}. Bu ob-havo o'lchovi, kasallikning "
            f"o'zi emas: issiq (21–29°) kunlar ko'paysa bosim o'sadi, "
            f"salqin va yomg'irda pasayadi.\n"
            f"Nima qilish: barg va g'ujumda kulrang-oq, un sepilganday "
            f"g'uborni tekshiring.\n"
            f"Model (agronom uchun): Gubler–Thomas indeksining yadrosi "
            f"(UC Davis), indeks {idx}/100 {n} kun bo'yicha: kunda ≥6 "
            f"soat 21–29° bo'lsa +20, bo'lmasa −10; qator boshida noldan "
            f"boshlanadi, askospora fazasi va issiqlik sbrosisiz ehtiyot "
            f"tomonga yuqori." if uz else
            f"🌫 Оидиум\n"
            f"Давление болезни: {cat}. Это мера погоды, а не сама "
            f"болезнь: чем больше жарких (21–29°) дней, тем выше "
            f"давление; в прохладу и дождь оно падает.\n"
            f"Что делать: проверьте серо-белый, как присыпанный мукой, "
            f"налёт на листьях и гроздях.\n"
            f"Модель (для агронома): ядро индекса Gubler–Thomas "
            f"(UC Davis), индекс {idx}/100 по {n} дням: день с ≥6 ч при "
            f"21–29° даёт +20, иначе −10; стартует с нуля в начале ряда, "
            f"без фазы аскоспор и жаро-сброса смещён в сторону "
            f"предупреждения.")

    elif crop_key in ("peach", "apricot", "cherry"):
        (bm, bd), (em, ed) = BLOOM[crop_key]
        lo, hi = date(year, bm, bd), date(year, em, ed)
        blk = ["🌸 Monilioz (gul kuyishi) — faqat fon:" if uz
               else "🌸 Монилиоз (ожог цветков) — только фон:"]
        if now.date() < lo:
            blk.append(f"Xavf gullashda ({_dm(lo)}–{_dm(hi)}, kalendar "
                       "bo'yicha)." if uz else
                       f"Риск — в цветение ({_dm(lo)}–{_dm(hi)}, по "
                       "календарю).")
        elif now.date() <= hi + timedelta(days=7):
            wet_h = _wet_hours_between(hours, lo, min(hi, now.date()))
            blk.append(f"Gullash davrida {wet_h} soat barg namligi." if uz
                       else f"За цветение — {wet_h} ч листовой влажности.")
        else:
            blk.append("Gullash o'tdi; mevada chirish ko'rsangiz — "
                       "agronomga ko'rsating." if uz else
                       "Цветение прошло; гниль на плодах — повод показать "
                       "агроному.")
        blk.append(
            "Belgisi: gullar va yosh novdalar qo'ng'irlashib quriydi va "
            "daraxtda osilib qoladi — ko'rsangiz, agronomga ko'rsating."
            if uz else
            "Признак: цветки и молодые побеги буреют, усыхают и остаются "
            "висеть на дереве — увидите, покажите агроному.")
        blk.append(
            "Model (agronom uchun): 20–25°da 3–5 soat namlik yetishi "
            "mumkin, ammo yagona jadval manbalarda yo'q — shuning uchun "
            "holat bo'yalmaydi." if uz
            else "Модель (для агронома): при 20–25° может хватить 3–5 ч "
            "влажности, но единой таблицы в источниках нет — поэтому "
            "статус не красится.")
        parts.append("\n".join(blk))
        if crop_key == "peach" and now.month in (2, 3):
            parts.append(
                "🍑 Barg jingalakligi: kurtak bo'rtishida salqin-nam "
                "kunlar qulay — faqat fon." if uz else
                "🍑 Курчавость листьев: в набухание почек благоприятны "
                "прохладные мокрые дни — только фон.")

    elif crop_key in ("winter_wheat", "barley"):
        (bm, bd), (em, ed) = BLOOM[crop_key]
        lo, hi = date(year, bm, bd), date(year, em, ed)
        wet, total = humid_days(hours, lo, min(hi, now.date()))
        blk = ["🌾 Boshoq fuzariozi — faqat fon:" if uz
               else "🌾 Фузариоз колоса — только фон:"]
        if total:
            blk.append(f"Boshoqlash oynasida nam kunlar: {wet}/{total} "
                       f"(kun ≥6 soat RH≥90% bo'lsa nam)." if uz else
                       f"Влажных дней в окне колошения: {wet}/{total} "
                       f"(день влажный при ≥6 ч RH≥90%).")
        else:
            blk.append(f"Boshoqlash oynasi ({_dm(lo)}–{_dm(hi)}, "
                       "kalendar) qator bilan kesishmadi." if uz else
                       f"Окно колошения ({_dm(lo)}–{_dm(hi)}, календарь) "
                       "с рядом не пересеклось.")
        blk.append("Belgisi: oqargan boshoqlar yoki boshoqchalarda "
                   "pushti g'ubor." if uz else
                   "Признак: белёсые колосья или розоватый налёт на "
                   "колосках.")
        blk.append("Modellar chegaralari manbalarda farq qiladi — holat "
                   "bo'yalmaydi." if uz else
                   "Пороги моделей в источниках расходятся — статус не "
                   "красится.")
        parts.append("\n".join(blk))

    elif crop_key == "onion":
        wet, total = wet_nights(hours, now)
        parts.append(
            f"🧅 Peronosporoz — faqat fon: nam tunlar {wet}/{total} "
            f"(tunda ≥3 soat RH≥95%). Belgisi: patlarda kulrang-binafsha "
            "g'ubor, rangi o'chgan dog'lar. To'liq DOWNCAST modeli "
            "solishtirilmagan." if uz else
            f"🧅 Пероноспороз — только фон: влажных ночей {wet}/{total} "
            f"(ночью ≥3 ч RH≥95%). Признак: серо-фиолетовый налёт и "
            "блёклые пятна на перьях. Полную модель DOWNCAST не сверяли.")

    else:  # cucumber, melon, watermelon
        wet, total = wet_nights(hours, now)
        parts.append(
            f"🥒 Peronosporoz — faqat fon: nam tunlar {wet}/{total}. "
            "Belgisi: bargda burchakli sariq dog'lar, ostida kulrang "
            "g'ubor. Asosiy omil — sporalarning shamol bilan kelishi; "
            "mahalliy ob-havo uni ko'rmaydi, shuning uchun holat "
            "bo'yalmaydi." if uz
            else
            f"🥒 Пероноспороз — только фон: влажных ночей {wet}/{total}. "
            "Признак: угловатые жёлтые пятна на листе, серый налёт "
            "снизу. Главный фактор — прилёт спор с ветром; локальная "
            "погода его не видит, поэтому статус не красится.")

    parts.append(_final_disclaimer(uz))
    return "\n\n".join(parts)
