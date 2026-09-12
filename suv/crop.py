"""
Crop coefficients (Kc) and growth stages — FAO-56 chapters 6-7.

Stage lengths and Kc values are FAO-56 Table 11/12 defaults, adjusted to
Uzbek planting calendars. THESE ARE DEFAULTS, NOT MEASUREMENTS. Before
the pilot goes past demo, an agronomist must replace them with values
from the local extension service. Flagged loudly on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Crop:
    key: str
    name_uz: str
    name_ru: str
    # stage lengths in days: initial, development, mid, late
    stages: tuple[int, int, int, int]
    kc_ini: float
    kc_mid: float
    kc_end: float
    root_depth_m: float  # max effective rooting depth
    depletion_fraction: float  # p — fraction of TAW allowed before stress
    typical_sowing: tuple[int, int]  # (month, day) — sowing, or bud break
    perennial: bool = False  # tree/vine: the cycle restarts every spring
    # Как переводить NDVI в Kc (см. kc_from_ndvi):
    #   "linear" — Kcb = 1.44·NDVI − 0.10 (Campos et al. 2010 на виноградниках,
    #              обобщено Calera et al. 2017): однолетние культуры и лоза;
    #   "cover"  — по доле покрытия и высоте кроны (Allen & Pereira 2009):
    #              деревья, у которых крона над голым междурядьем.
    ndvi_kc_model: str = "linear"
    # Геометрия кроны для модели "cover": высота, м, и множитель ML —
    # насколько транспирация кроны опережает долю затенённой земли
    # (деревья 1,5-2,0).
    canopy_height_m: float = 0.0
    canopy_ml: float = 1.5
    # За сколько дней ДО начала съёма останавливать полив, когда дата
    # съёма известна (Field.harvest_start). Налитый водой перед съёмом
    # плод мягче и хуже лежит в хранении; стандартная садоводческая
    # практика — сухая пауза в 1-2 недели перед теримом, а не «без воды
    # с сентября». Работает только при заданной дате съёма.
    preharvest_hold_days: int = 10


# FAO-56 Table 11 (stage lengths), Table 12 (Kc), Table 22 (Zr, p).
CROPS: dict[str, Crop] = {
    "cotton": Crop(
        key="cotton", name_uz="Paxta", name_ru="Хлопок",
        stages=(30, 50, 60, 55),
        kc_ini=0.35, kc_mid=1.15, kc_end=0.60,
        root_depth_m=1.35, depletion_fraction=0.65,
        typical_sowing=(4, 10),
    ),
    "winter_wheat": Crop(
        key="winter_wheat", name_uz="Kuzgi bug'doy", name_ru="Озимая пшеница",
        stages=(30, 140, 40, 30),
        kc_ini=0.40, kc_mid=1.15, kc_end=0.35,
        root_depth_m=1.50, depletion_fraction=0.55,
        typical_sowing=(10, 5),
    ),
    "tomato": Crop(
        key="tomato", name_uz="Pomidor", name_ru="Помидор",
        stages=(30, 40, 45, 30),
        kc_ini=0.60, kc_mid=1.15, kc_end=0.80,
        root_depth_m=1.00, depletion_fraction=0.40,
        typical_sowing=(4, 25),
    ),
    "onion": Crop(
        key="onion", name_uz="Piyoz", name_ru="Лук",
        stages=(20, 35, 110, 45),
        kc_ini=0.70, kc_mid=1.05, kc_end=0.75,
        root_depth_m=0.45, depletion_fraction=0.30,
        typical_sowing=(3, 15),
    ),
    "apple": Crop(
        key="apple", name_uz="Olma", name_ru="Яблоня",
        stages=(30, 50, 130, 30),
        kc_ini=0.45, kc_mid=0.95, kc_end=0.70,
        root_depth_m=1.50, depletion_fraction=0.50,
        typical_sowing=(3, 20),  # bud break, not sowing
        perennial=True,
        # Крона над голым междурядьем — Kc по доле покрытия.
        ndvi_kc_model="cover", canopy_height_m=3.0, canopy_ml=1.5,
        preharvest_hold_days=14,
    ),
    "grape": Crop(
        key="grape", name_uz="Uzum", name_ru="Виноград",
        stages=(20, 50, 75, 60),
        kc_ini=0.30, kc_mid=0.85, kc_end=0.45,
        root_depth_m=1.50, depletion_fraction=0.45,
        typical_sowing=(4, 1),  # bud break
        perennial=True,
        # Линейная связь NDVI->Kcb выведена именно на виноградниках
        # (Campos et al. 2010), поэтому лозе она оставлена.
        ndvi_kc_model="linear", canopy_height_m=2.0,
    ),
    # Три культуры фазы 1 (11.09.2026) — по структуре посевов страны
    # (косточковые, люцерна, ячмень закрывают вместе с прежней шестёркой
    # ~65% орошаемой площади) и под клинья гиганта из Самарканда.
    # Значения — те же FAO-56 табл. 11/12/22, НЕ полевые измерения.
    "apricot": Crop(
        key="apricot", name_uz="O'rik", name_ru="Абрикос",
        # Листва с распускания в марте до осени; съём в июне-июле
        # закрывается режимом терима, а не формой кривой Kc.
        stages=(20, 70, 90, 30),
        kc_ini=0.45, kc_mid=0.90, kc_end=0.65,   # табл. 12: косточковые без задернения
        root_depth_m=1.50, depletion_fraction=0.50,
        typical_sowing=(3, 15),  # распускание почек
        perennial=True,
        ndvi_kc_model="cover", canopy_height_m=3.0, canopy_ml=1.5,
    ),
    "alfalfa": Crop(
        key="alfalfa", name_uz="Beda", name_ru="Люцерна",
        # «Averaged cutting effects» из табл. 12: Kc усреднён по укосам,
        # отдельные укосы движок не моделирует — и говорит об этом здесь.
        stages=(10, 30, 160, 20),
        kc_ini=0.40, kc_mid=0.95, kc_end=0.90,
        root_depth_m=1.50, depletion_fraction=0.55,
        typical_sowing=(3, 10),  # отрастание весной
        perennial=True,          # травостой: NDVI->Kc линейная, родная Calera
    ),
    "barley": Crop(
        key="barley", name_uz="Arpa", name_ru="Ячмень",
        # Озимый, как пшеница, но короче и с более сухим финишем.
        stages=(30, 130, 40, 25),
        kc_ini=0.40, kc_mid=1.15, kc_end=0.25,
        root_depth_m=1.30, depletion_fraction=0.55,
        typical_sowing=(10, 1),
    ),
}

STAGE_NAMES_UZ = ("Boshlang'ich", "Rivojlanish", "O'rta", "Yakuniy")
STAGE_NAMES_RU = ("Начальная", "Развитие", "Средняя", "Завершающая")


@dataclass
class StageInfo:
    index: int  # 0..3
    name_uz: str
    name_ru: str
    days_after_planting: int
    kc: float


def season_start(crop: Crop, planting: date, today: date) -> date:
    """
    Начало ТЕКУЩЕГО вегетационного цикла.

    Для однолетних это дата сева. Для многолетних — распускание почек
    этой весны: сад, посаженный девять лет назад, каждый год проходит
    цикл заново, и считать «дни от посадки» бессмысленно.

    Ошибка настоящая, не гипотетическая: виноградник 2017 года давал
    Kc 0.45 вместо 0.85 — почти двукратное занижение потребности поля в
    середине августа, то есть совет поливать вдвое реже, чем нужно.
    """
    if not crop.perennial:
        return planting

    month, day = crop.typical_sowing
    this_year = date(today.year, month, day)
    if today >= this_year:
        return this_year
    return date(today.year - 1, month, day)


def days_after_planting(planting: date, today: date) -> int:
    return (today - planting).days


def sowing_from_month(crop: Crop, month: int, today: date) -> date:
    """
    Названный фермером месяц -> дата сева.

    Мастер спрашивает «в каком месяце ПОСЕЯЛИ», прошедшим временем, —
    значит дата сева не может оказаться в будущем. Прежнее правило
    `year = today.year if month <= today.month else today.year - 1`
    ломалось на озимых с обеих сторон. 12.09.2026, ячмень (сев 1
    октября): ответ «Oktabr» давал 01.10.2025 — dap 346 при сумме стадий
    225, то есть ПРОШЛЫЙ, уже убранный сезон; ответ «Sentabr» давал
    15.09.2026, на три дня вперёд, и карточка показывала начальную
    стадию непосеянного поля.

    Правило одно для озимых и яровых: берём ПОСЛЕДНЕЕ прошедшее
    наступление названного месяца. Если день по умолчанию ещё не настал,
    а месяц — текущий, значит фермер посеял в этом месяце раньше этого
    числа: берём сегодня. Ошибка тогда не больше уже прошедшей части
    месяца — меньше тех ±10 дней, которые мастер и так допускает.

    Кончившийся сезон этим НЕ чинится и не должен: «Oktabr» в сентябре —
    честно прошлогодний сев, и сказать об этом обязан recommend()
    отказом, а не молчаливый kc_end (см. season_is_over).
    """
    day = crop.typical_sowing[1] if month == crop.typical_sowing[0] else 15
    this_year = date(today.year, month, day)
    if this_year <= today:
        return this_year
    if month == today.month:
        return today
    return date(today.year - 1, month, day)


def season_is_over(crop: Crop, dap: int) -> bool:
    """Сезон однолетней культуры кончился: дней от сева больше суммы
    стадий.

    За этой границей stage_and_kc уходит в else и молча отдаёт kc_end —
    кривая держится вечно, и движок считает воду по стерне. Многолетники
    сюда не попадают по определению: season_start перезапускает им
    отсчёт каждой весной, зима у сада — межсезонье, а не конец сезона, и
    послеуборочный полив под будущие почки остаётся законным.
    """
    return not crop.perennial and dap >= sum(crop.stages)


def stage_and_kc(crop: Crop, dap: int) -> StageInfo:
    """
    Kc for a given day after planting. FAO-56 fig. 25: flat during
    initial and mid, linear interpolation through development and late.
    """
    ini, dev, mid, late = crop.stages
    dap = max(dap, 0)

    if dap < ini:
        kc, idx = crop.kc_ini, 0
    elif dap < ini + dev:
        frac = (dap - ini) / dev
        kc, idx = crop.kc_ini + frac * (crop.kc_mid - crop.kc_ini), 1
    elif dap < ini + dev + mid:
        kc, idx = crop.kc_mid, 2
    elif dap < ini + dev + mid + late:
        frac = (dap - ini - dev - mid) / late
        kc, idx = crop.kc_mid + frac * (crop.kc_end - crop.kc_mid), 3
    else:
        kc, idx = crop.kc_end, 3

    return StageInfo(
        index=idx,
        name_uz=STAGE_NAMES_UZ[idx],
        name_ru=STAGE_NAMES_RU[idx],
        days_after_planting=dap,
        kc=round(kc, 3),
    )


# Сколько лет дереву или лозе нужно, чтобы корни дошли до паспортной
# глубины. До этого возраста корни растут с возрастом САЖЕНЦА, а не с
# днями от распускания почек.
PERENNIAL_ESTABLISHED_YEARS = 3.0


def root_depth(crop: Crop, dap: int, years_since_planting: float | None = None) -> float:
    """
    Effective rooting depth, m.

    Annuals: grows linearly from 0.20 m at emergence to the maximum by the
    end of the development stage, then holds (FAO-56 eq. 8-1 simplified).

    Perennials: FAO-56 treats Zr of established trees and vines as CONSTANT
    at its maximum — roots do not regrow each spring. The old code restarted
    from 0.20 m at every bud break (dap is counted from this year's season
    start), so a nine-year-old orchard got TAW 60 mm instead of 195 in
    April and was told to irrigate ~3x too often, in small doses; the root
    zone then «collapsed» 1.5 -> 0.2 m mid-simulation and the growth-
    dilution step erased accumulated deficit. Found by the 18.08.2026
    audit (crop.py:155). Young plantings (< PERENNIAL_ESTABLISHED_YEARS)
    scale with the age of the sapling; unknown age = established.
    """
    if crop.perennial:
        if years_since_planting is None or years_since_planting >= PERENNIAL_ESTABLISHED_YEARS:
            return crop.root_depth_m
        frac = max(0.0, years_since_planting) / PERENNIAL_ESTABLISHED_YEARS
        return max(0.40, frac * crop.root_depth_m)

    ini, dev, _, _ = crop.stages
    z_min = 0.20  # m, at emergence
    if dap >= ini + dev:
        return crop.root_depth_m
    frac = min(max(dap / (ini + dev), 0.0), 1.0)
    return z_min + frac * (crop.root_depth_m - z_min)


# NDVI голой почвы и сомкнутого полога — для перевода NDVI в долю
# покрытия (Carlson & Ripley 1997: fc = ((NDVI-NDVI_s)/(NDVI_v-NDVI_s))).
# Значения обычные для сельхозземель; на светлом солончаке NDVI_s ниже,
# но конверт культуры ниже всё равно не пускает.
NDVI_BARE_SOIL = 0.15
NDVI_FULL_COVER = 0.85
# Те же границы для MSAVI (MSAVI2, Qi et al. 1994). MSAVI гасит вклад
# яркости почвы, поэтому на редком пологе — сад с голым междурядьем,
# молодой хлопок — доля покрытия по нему устойчивее: тёмная мокрая
# земля после полива не рисует «прирост кроны», светлая сухая не
# стирает его. Диапазон литературный, не полевая калибровка: у
# сомкнутого полога MSAVI насыщается ниже NDVI.
MSAVI_BARE_SOIL = 0.10
MSAVI_FULL_COVER = 0.75
# Kc почти голой почвы между поливами (FAO-56, Kc_min).
KC_MIN = 0.15


def fraction_cover(ndvi: float, msavi: float | None = None) -> float:
    """Доля земли под кроной, 0..1.

    Если у кадра есть MSAVI — считаем по нему (почвенный фон подавлен),
    NDVI остаётся точкой отсчёта для источников без MSAVI (Landsat-резерв,
    старые записи журнала)."""
    if msavi is not None:
        fc = (msavi - MSAVI_BARE_SOIL) / (MSAVI_FULL_COVER - MSAVI_BARE_SOIL)
    else:
        fc = (ndvi - NDVI_BARE_SOIL) / (NDVI_FULL_COVER - NDVI_BARE_SOIL)
    return min(max(fc, 0.0), 1.0)


def kc_from_ndvi(ndvi: float, crop: Crop, msavi: float | None = None) -> float:
    """
    Kc estimated from satellite NDVI.

    Two relations, chosen per crop (Crop.ndvi_kc_model) — the split is
    the point:

    * "linear" — Kcb = 1.44 * NDVI - 0.10. Derived on drip-irrigated
      vineyards (Campos et al. 2010) and generalised to row crops
      (Calera et al. 2017). Used for annuals and for grapes, where it
      was actually validated.

    * "cover" — tree crops. On an orchard the same line UNDER-reads: a
      crown over a bare inter-row has NDVI 0.4-0.5 at full leaf, and the
      line turns that into Kc 0.5-0.6 — as if half the orchard were
      fallow — whereas a tree transpires more per unit of shaded ground
      than a herbaceous canopy (taller, rougher, more aerodynamic
      coupling). Allen & Pereira (2009, Irrig. Sci. 28) give the density
      form FAO uses for tree crops:
          Kd  = min(1, ML*fc, fc^(1/(1+h)))
          Kcb = Kc_min + Kd * (Kcb_full - Kc_min)
      with fc the fraction of ground covered (from NDVI), h the canopy
      height and ML the crown multiplier. Kcb_full is taken as the top of
      the crop's own envelope (kc_mid * 1.05). For Farrukh's orchard at
      NDVI 0.47 this gives 0.73 against Calera's 0.57 — and with it the
      engine's interval lands on the 4 days the farmer actually keeps.

    Neither is a MEASURED Kc for these fields: no lysimeter, no soil
    moisture probe. Both are literature models, and the satellite weight
    in blended_kc() caps their influence at 70%.

    Clamped into the crop's own Kc envelope so a bad pixel cannot produce
    a nonsense recommendation. This is the layer that makes the system
    per-field rather than per-district: the crop calendar says what
    SHOULD be happening, the satellite says what IS happening.
    """
    # Clamp against the UNROUNDED envelope, then round for display.
    # Rounding the bounds themselves can push the result a hair past the
    # limit, which quietly defeats the guard.
    lo, hi = crop.kc_ini * 0.6, crop.kc_mid * 1.05
    if crop.ndvi_kc_model == "cover":
        fc = fraction_cover(ndvi, msavi)
        kd = min(1.0, crop.canopy_ml * fc,
                 fc ** (1.0 / (1.0 + crop.canopy_height_m)) if fc > 0 else 0.0)
        kcb = KC_MIN + kd * (hi - KC_MIN)
    else:
        kcb = 1.44 * ndvi - 0.10
    return round(min(max(kcb, lo), hi), 4)


def blended_kc(calendar_kc: float, ndvi_kc: float | None,
               ndvi_age_days: int = 0) -> tuple[float, str]:
    """
    Combine calendar Kc with satellite Kc.

    Same-day imagery is trusted at 70%; the weight decays LINEARLY from
    day 0 to zero at 14 days (a 5-day-old scene weighs 45%, a 12-day-old
    one 10%), because a two-week-old NDVI in the middle of rapid canopy
    development is worse than the calendar. An earlier docstring promised
    a «<=5 days at 70%» plateau the formula never had — the 18.08.2026
    audit caught the mismatch; the formula is the intended behaviour and
    kc_source prints the weight actually applied.
    """
    if ndvi_kc is None:
        return calendar_kc, "calendar"
    if ndvi_age_days > 14:
        return calendar_kc, "calendar (imagery stale)"

    # Возраст бывает и отрицательным: warm-start гонит симуляцию по дням
    # ДО даты снимка, и без ограничения сверху вес превышал 100% —
    # blended_kc(0.95, 0.55, -11) давал 0.45, ниже ОБОИХ источников.
    # Снимок из будущего относительно моделируемого дня весит как свежий.
    weight = 0.70 * max(0.0, min(1.0, 1.0 - ndvi_age_days / 14.0))
    kc = calendar_kc * (1 - weight) + ndvi_kc * weight
    return round(kc, 3), f"calendar+satellite ({round(weight * 100)}% satellite)"
