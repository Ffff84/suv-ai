"""
Экономика полива: кубы → киловатты → сумы.

Появилось после первого разговора с реальным фермером. Он назвал главной
болью не воду, а счёт за электричество — и оказался прав: при подъёме с
глубокой скважины электричество и есть основная статья затрат на полив.

Считать экономию в кубометрах — значит говорить на языке, который никого
не убеждает. Фермер, директор кластера и министерство одинаково понимают
сумы. Вода — это промежуточная единица, а не результат.
"""

from __future__ import annotations

from dataclasses import dataclass

WATER_DENSITY = 1000.0  # кг/м³
G = 9.81  # м/с²


@dataclass(frozen=True)
class PumpProfile:
    """
    Замеренная характеристика насосной установки хозяйства.

    Задаётся ИЗМЕРЕНИЯМИ, а не паспортом насоса: паспортный КПД и
    фактический расходятся в разы, особенно на изношенном оборудовании.
    Достаточно трёх чисел, которые фермер знает: сколько киловатт за час,
    сколько кубов за час, сколько стоит час.
    """

    kwh_per_hour: float
    m3_per_hour: float
    cost_per_hour_uzs: float
    lift_m: float = 0.0  # глубина подъёма, для оценки КПД

    @property
    def kwh_per_m3(self) -> float:
        return self.kwh_per_hour / self.m3_per_hour

    @property
    def uzs_per_m3(self) -> float:
        return self.cost_per_hour_uzs / self.m3_per_hour

    @property
    def uzs_per_kwh(self) -> float:
        return self.cost_per_hour_uzs / self.kwh_per_hour

    @property
    def theoretical_kwh_per_m3(self) -> float:
        """Чистая физика подъёма: ρgh. Ниже этого не бывает."""
        return WATER_DENSITY * G * self.lift_m / 3.6e6

    @property
    def efficiency(self) -> float | None:
        """
        Доля энергии, ушедшая на подъём воды. Остальное — тепло, трение,
        износ. Исправная установка даёт 0.50-0.70.
        """
        if self.lift_m <= 0:
            return None
        return self.theoretical_kwh_per_m3 / self.kwh_per_m3


GRAVITY = None  # самотёк из канала: электричества нет


@dataclass
class Cost:
    m3: float
    kwh: float
    uzs: float
    hours: float


def cost_of(volume_m3: float, pump: PumpProfile | None) -> Cost:
    """
    Стоимость объёма воды. pump=None означает самотёк из канала.

    Самотёк надо было выделить отдельно, потому что он ломает всю
    аргументацию про деньги: у такого поля вода бесплатна для фермера,
    и «сэкономили 500 000 сум» там просто неправда. Экономия есть, но
    достаётся она государству и соседям ниже по каналу, а не хозяину
    поля. Это два разных клиента и два разных разговора.
    """
    if pump is None:
        return Cost(m3=round(volume_m3, 1), kwh=0.0, uzs=0, hours=0.0)
    return Cost(
        m3=round(volume_m3, 1),
        kwh=round(volume_m3 * pump.kwh_per_m3, 1),
        uzs=round(volume_m3 * pump.uzs_per_m3),
        hours=round(volume_m3 / pump.m3_per_hour, 1),
    )


def savings(actual_m3: float, recommended_m3: float,
            pump: PumpProfile | None) -> Cost:
    """
    Разница между тем, что фермер вылил, и тем, что было нужно.

    Может быть отрицательной — и это не ошибка, а результат: недополив
    так же реален, как перелив, и стоит урожая. Система, которая умеет
    показывать только экономию, врёт по построению.
    """
    return cost_of(actual_m3 - recommended_m3, pump)


def pump_upgrade_saving(annual_m3: float, pump: PumpProfile,
                        target_efficiency: float = 0.60) -> Cost | None:
    """
    Сколько даст замена самого насоса, без изменения режима полива.

    Отдельная от расписания величина, и путать их нельзя: одна экономия
    достигается софтом, другая — капитальными вложениями. Смешивать их в
    одну цифру на слайде — это ровно тот вид завышения, который жюри
    ловит первым.
    """
    if pump.lift_m <= 0:
        return None
    better_kwh_per_m3 = pump.theoretical_kwh_per_m3 / target_efficiency
    delta = pump.kwh_per_m3 - better_kwh_per_m3
    if delta <= 0:
        return None
    kwh = annual_m3 * delta
    return Cost(m3=0.0, kwh=round(kwh, 1),
                uzs=round(kwh * pump.uzs_per_kwh), hours=0.0)


def value_story(pump: PumpProfile | None) -> str:
    """Кому достаётся выгода от экономии на этом поле."""
    if pump is None:
        return ("самотёк: вода фермеру ничего не стоит — выгода достаётся "
                "государству и хозяйствам ниже по каналу")
    return "насос: экономия воды сразу превращается в деньги фермера"


def describe(pump: PumpProfile | None) -> str:
    if pump is None:
        return "самотёк из канала · электричество не расходуется"
    parts = [
        f"{pump.kwh_per_m3:.2f} кВт·ч/м³",
        f"{pump.uzs_per_m3:,.0f} сум/м³".replace(",", " "),
        f"{pump.uzs_per_kwh:,.0f} сум/кВт·ч".replace(",", " "),
    ]
    eff = pump.efficiency
    if eff is not None:
        parts.append(f"КПД установки ≈ {eff*100:.0f}%")
    return " · ".join(parts)
