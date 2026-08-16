"""
Фото поля со спутника: снимок Sentinel-2 и измеренная влажность поверх.

Отличие от ТЗ FieldStatus §4.3, и оно принципиальное. Там заливка
строится по модели продвижения воды вдоль борозды и опирается на `reach`
— замер, докуда вода дошла. Ни `reach`, ни данных для модели у пилота
нет: расход канала никто не меряет, уклон из рельефа не берётся (30 м на
пиксель дают уклон долины, а не поля), ТЗ модуля Egat не существует.

Поэтому красим то, что спутник ИЗМЕРИЛ: индекс влажности NDMI по
пикселям поля. Это не обход правила §1.1, а его соблюдение — правило
запрещает рисовать карту по расчёту и прямо разрешает строить её по
замеру, называя NDMI одним из двух допустимых источников.

Цена честности: NDMI считается по коротковолновому каналу с нативным
разрешением 20 м, вдвое грубее подложки, и падает не только от сухости,
но и от разреженности растительности. Поэтому модуль отказывается
рисовать градиент там, где разброс мал: ровное поле не должно
раскрашиваться в красное и зелёное только потому, что шкалу растянули.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

# Ниже этого разброса NDMI разница по полю неотличима от шума прибора и
# от пестроты самой листвы. Красить такое поле градиентом — значит
# показывать фермеру узор, которого нет.
MIN_SPREAD = 0.08

# Прозрачность заливки: снимок под ней должен читаться, иначе фермер не
# узнает своё поле и не поверит картинке.
OVERLAY_ALPHA = 0.55

# Сухая зона делается плотнее — красный на тёмной пашне иначе теряется.
DRY_ALPHA_BOOST = 0.16

# Доля чистых от облаков пикселей ВНУТРИ поля, при которой кадр годится.
# Считать по всей сцене нельзя: облако за 40 км от поля выбраковало бы
# совершенно годный снимок.
MIN_VALID_FRACTION = 0.80

# Больше двух недель без чистого кадра — показывать старый снимок как
# сегодняшний нельзя (ТЗ §1.3).
MAX_SCENE_AGE_DAYS = 14

# Поле мельче трёх гектаров при 10 м на пиксель — это квадрат 17x17
# точек, зон там не разглядеть (ТЗ §1.4).
MIN_AREA_HA = 3.0


@dataclass(frozen=True)
class MoistureStats:
    """Что показал замер влажности внутри контура поля."""

    low: float          # NDMI самой сухой части
    high: float         # NDMI самой влажной
    mean: float
    valid_fraction: float
    pixels: int

    @property
    def spread(self) -> float:
        return self.high - self.low

    @property
    def uniform(self) -> bool:
        """Разброс так мал, что говорить о сухих и влажных зонах нельзя."""
        return self.spread < MIN_SPREAD


def stats_over_field(values, inside, cloud_free) -> MoistureStats | None:
    """Свести замер по пикселям внутри контура.

    Три сетки одного размера: значения NDMI, признак «пиксель внутри
    поля» и признак «пиксель не закрыт облаком».

    Доля чистых считается от площади ПОЛЯ, а не от всего кадра. Иначе
    она мерила бы, какую часть прямоугольника занимает участок, и поле
    без единого облака отбраковывалось бы как закрытое (ТЗ §4.2).
    """
    picked = [v for v, ins, clear in zip(_flat(values), _flat(inside),
                                         _flat(cloud_free)) if ins and clear]
    in_field = sum(1 for ins in _flat(inside) if ins)
    if not picked or not in_field:
        return None
    return MoistureStats(
        low=min(picked), high=max(picked),
        mean=sum(picked) / len(picked),
        valid_fraction=len(picked) / in_field, pixels=len(picked))


def _flat(grid):
    for row in grid:
        for cell in row:
            yield cell


def moisture_color(value: float, stats: MoistureStats) -> tuple[int, int, int, int]:
    """Цвет пикселя по его влажности: красный — сухо, зелёный — влажно.

    Шкала растягивается на диапазон САМОГО ПОЛЯ, а не на абсолютный:
    абсолютные пороги NDMI зависят от культуры и стадии, и одна и та же
    цифра на винограднике и на хлопке значит разное. Вопрос фермера —
    «где у меня суше», а не «сколько это в единицах индекса».
    """
    t = 0.5 if stats.spread <= 0 else (value - stats.low) / stats.spread
    t = max(0.0, min(1.0, t))
    # красный -> жёлтый -> зелёный
    if t < 0.5:
        k = t / 0.5
        r, g, b = 220, int(60 + 150 * k), 40
    else:
        k = (t - 0.5) / 0.5
        r, g, b = int(220 - 160 * k), int(210 - 10 * k), 40
    alpha = OVERLAY_ALPHA + DRY_ALPHA_BOOST * (1.0 - t)
    return r, g, b, int(255 * min(1.0, alpha))


@dataclass(frozen=True)
class PhotoVerdict:
    """Можно ли показать фото и, если нет, почему."""

    ok: bool
    reason: str | None          # ключ для i18n
    scene_age_days: int | None = None


def can_show_photo(area_ha: float | None, irrigation_method: str,
                   has_polygon: bool, scene_day: date | None,
                   today: date, valid_fraction: float | None) -> PhotoVerdict:
    """Правила ТЗ §4.6, по порядку, каждое со своей причиной.

    Причина возвращается ключом, а не текстом: фермеру в секции нужна
    короткая строка на его языке и кнопка к недостающему шагу.
    """
    if not has_polygon:
        return PhotoVerdict(False, "no_contour")
    if irrigation_method != "furrow":
        return PhotoVerdict(False, "not_furrow")
    if not area_ha or area_ha < MIN_AREA_HA:
        return PhotoVerdict(False, "too_small")
    if scene_day is None:
        return PhotoVerdict(False, "no_scene")
    age = (today - scene_day).days
    if age > MAX_SCENE_AGE_DAYS:
        return PhotoVerdict(False, "scene_stale", age)
    if valid_fraction is not None and valid_fraction < MIN_VALID_FRACTION:
        return PhotoVerdict(False, "clouded", age)
    return PhotoVerdict(True, None, age)


# ---------------------------------------------------------------- геометрия

def bbox_of(ring: list[list[float]], pad: float = 0.15
            ) -> tuple[float, float, float, float]:
    """Оборачивающий прямоугольник контура с отступом (ТЗ §4.2).

    Отступ нужен, чтобы поле не упиралось в край кадра: фермер узнаёт
    свой участок по соседям, дороге и каналу вокруг него.
    """
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    dlon = (max(lons) - min(lons)) * pad
    dlat = (max(lats) - min(lats)) * pad
    return (min(lons) - dlon, min(lats) - dlat,
            max(lons) + dlon, max(lats) + dlat)


def ring_to_pixels(ring: list[list[float]],
                   box: tuple[float, float, float, float],
                   width: int, height: int) -> list[tuple[float, float]]:
    """Контур из градусов в пиксели кадра. Ось Y в картинке идёт вниз."""
    lon0, lat0, lon1, lat1 = box
    sx = width / (lon1 - lon0) if lon1 != lon0 else 0.0
    sy = height / (lat1 - lat0) if lat1 != lat0 else 0.0
    return [((lon - lon0) * sx, (lat1 - lat) * sy) for lon, lat in ring]


def scale_bar_m(box: tuple[float, float, float, float]) -> int:
    """Круглая длина масштабной линейки под ширину кадра."""
    lon0, lat0, lon1, lat1 = box
    mid = math.radians((lat0 + lat1) / 2.0)
    width_m = (lon1 - lon0) * 111_320.0 * math.cos(mid)
    for step in (500, 200, 100, 50, 20):
        if width_m / 4 >= step:
            return step
    return 10
