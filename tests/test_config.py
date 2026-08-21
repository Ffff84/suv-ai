"""
.env must load on every platform. This was a real bug: the project
shipped a .env.example but nothing that read a .env, so on Windows —
where `export` does not exist — there was nowhere to put the keys at all.
"""
import os
from pathlib import Path

from suv.config import find_env, load_env


def test_reads_plain_pairs(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("A_KEY=hello\nB_KEY=world\n", encoding="utf-8")
    monkeypatch.delenv("A_KEY", raising=False)
    got = load_env(tmp_path / ".env")
    assert got["A_KEY"] == "hello"
    assert os.environ["A_KEY"] == "hello"


def test_strips_quotes_and_export_prefix(tmp_path):
    (tmp_path / ".env").write_text(
        'export Q_KEY="quoted"\nS_KEY=\'single\'\n', encoding="utf-8")
    got = load_env(tmp_path / ".env", override=True)
    assert got["Q_KEY"] == "quoted"
    assert got["S_KEY"] == "single"


def test_survives_notepad_bom(tmp_path):
    """Windows Notepad writes a BOM that hides the first variable."""
    (tmp_path / ".env").write_text("FIRST_KEY=value\n", encoding="utf-8-sig")
    got = load_env(tmp_path / ".env", override=True)
    assert "FIRST_KEY" in got, got


def test_ignores_comments_and_blank_lines(tmp_path):
    (tmp_path / ".env").write_text(
        "# comment\n\nC_KEY=1\n   \n", encoding="utf-8")
    got = load_env(tmp_path / ".env", override=True)
    assert list(got) == ["C_KEY"]


def test_does_not_clobber_real_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("P_KEY", "from-shell")
    (tmp_path / ".env").write_text("P_KEY=from-file\n", encoding="utf-8")
    load_env(tmp_path / ".env")
    assert os.environ["P_KEY"] == "from-shell"


def test_missing_file_is_not_an_error(tmp_path):
    assert load_env(tmp_path / "nope.env") == {}


def test_finds_env_upwards(tmp_path):
    (tmp_path / ".env").write_text("U_KEY=1\n", encoding="utf-8")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert find_env(deep) == tmp_path / ".env"


# ---- satellite enrichment must never break the engine ----

def test_enrich_degrades_gracefully_without_credentials(monkeypatch):
    """
    Regression: the satellite layer was connected but never called from the
    recommendation path. Now that it is called, it must not be able to take
    the engine down — no keys, no network and full cloud cover all have to
    end in a plain calendar-based recommendation.
    """
    from datetime import date

    from suv.crop import CROPS
    from suv.enrich import attach_ndvi
    from suv.schedule import Field
    from suv.soil import SOILS

    monkeypatch.delenv("CDSE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CDSE_CLIENT_SECRET", raising=False)
    # Запасной источник тоже гасим: у него своих ключей нет, и с
    # выставленной LANDSAT_FALLBACK этот офлайн-тест ушёл бы в живую сеть.
    monkeypatch.delenv("LANDSAT_FALLBACK", raising=False)

    f = Field("T", "T", 1.0, 39.65, 66.96, 705, CROPS["cotton"], SOILS["loam"],
              date(2026, 4, 15), "furrow")
    status = attach_ndvi(f)
    assert f.ndvi is None
    assert "календар" in status


def test_enrich_survives_a_failing_satellite(monkeypatch):
    from datetime import date

    import suv.enrich as enrich
    from suv.crop import CROPS
    from suv.schedule import Field
    from suv.soil import SOILS

    monkeypatch.setenv("CDSE_CLIENT_ID", "x")
    monkeypatch.setenv("CDSE_CLIENT_SECRET", "y")
    monkeypatch.delenv("LANDSAT_FALLBACK", raising=False)
    monkeypatch.setattr(enrich, "get_token",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    f = Field("T", "T", 1.0, 39.65, 66.96, 705, CROPS["cotton"], SOILS["loam"],
              date(2026, 4, 15), "furrow")
    status = enrich.attach_ndvi(f)
    assert f.ndvi is None
    assert "авторизации" in status
