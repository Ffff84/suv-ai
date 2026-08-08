"""
Чтение .env без внешних зависимостей.

Отдельный модуль, потому что запускать проект приходится с трёх разных
входных точек (диагностика, расчёт поля, бот), и каждая должна видеть
ключи одинаково. Раньше ключи читались только из переменных окружения —
на Windows это означало, что положить их было практически некуда.

Сознательно без python-dotenv: на пилоте каждая лишняя зависимость это
ещё одна вещь, которая может не встать на чужой машине за два дня до
дедлайна.
"""

from __future__ import annotations

import os
from pathlib import Path


def find_env(start: Path | None = None) -> Path | None:
    """Ищем .env от текущей папки вверх до корня репозитория."""
    here = (start or Path.cwd()).resolve()
    for folder in [here, *here.parents]:
        candidate = folder / ".env"
        if candidate.is_file():
            return candidate
        if (folder / ".git").exists() or (folder / "suv").is_dir():
            break
    return None


def load_env(path: str | Path | None = None, override: bool = False) -> dict[str, str]:
    """
    Загрузить .env в os.environ. Возвращает то, что реально прочитано.

    Уже существующие переменные окружения по умолчанию НЕ перетираются:
    если человек явно выставил ключ в терминале, он ожидает, что победит
    именно он, а не забытый файл.
    """
    p = Path(path) if path else find_env()
    if p is None or not p.is_file():
        return {}

    loaded: dict[str, str] = {}
    # utf-8-sig: Блокнот на Windows пишет BOM, из-за которого первая
    # переменная получает невидимый префикс и просто не находится.
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key:
            continue
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def describe() -> str:
    """Короткая строка для диагностики: откуда взялись ключи."""
    p = find_env()
    return f"файл {p}" if p else "файл .env не найден"
