"""Tests for release discovery and for rewriting the configuration.

The config rewrite is the part worth testing hard: it edits the file that
governs every calibration in the project, and a sloppy edit could silently drop
a provenance record. These tests assert that it changes exactly the four source
keys and leaves everything else -- including every comment -- byte-identical.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest

from hutrackdb import sources
from hutrackdb.config import Config

ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = ROOT / "config" / "pipeline.yaml"


def _release(filename="hurdat2-1851-2099-03151700.txt", end=2099):
    return sources.Release(
        filename=filename,
        url=sources.NHC_INDEX + filename,
        start_season=1851,
        end_season=end,
        revised=dt.date(2100, 3, 15),
    )


# -- filename parsing -------------------------------------------------------

@pytest.mark.parametrize("stamp,expected", [
    ("042624", dt.date(2024, 4, 26)),      # MMDDYY
    ("02272026", dt.date(2026, 2, 27)),    # MMDDYYYY
])
def test_stamp_parsing_handles_both_date_widths(stamp, expected):
    assert sources._parse_stamp(stamp) == expected


@pytest.mark.parametrize("name", [
    "hurdat2-1851-2025-02272026.txt",
    "hurdat2-nepac-1949-2025-02272026.txt",
    "hurdat2-atl-1851-2023-042624.txt",
])
def test_release_pattern_accepts_published_filename_forms(name):
    assert sources._RELEASE_RE.match(name)


def test_release_pattern_rejects_names_without_a_season_range():
    # These cannot be ordered against the rest, so they are deliberately skipped.
    assert sources._RELEASE_RE.match("hurdat2-atl-02052024.txt") is None


def test_releases_order_by_season_then_revision_date():
    older = _release("hurdat2-1851-2024-040425.txt", end=2024)
    newer = _release("hurdat2-1851-2025-02272026.txt", end=2025)
    # The newer release has an EARLIER calendar stamp in its name only because
    # NOAA shifted its publication month; season coverage must win.
    assert sorted([newer, older], key=lambda r: r.sort_key) == [older, newer]


# -- the registry is the single source of truth -----------------------------

def test_registry_covers_every_source_the_build_needs():
    config = Config.load(REAL_CONFIG)
    names = [source.name for source in sources.registry(config)]
    assert "hurdat2 (atlantic)" in names
    assert "hurdat2 (pacific)" in names
    assert any("QA reference" in name for name in names)
    assert any("coastline" in name for name in names)


def test_registry_targets_and_urls_agree_with_the_configuration():
    config = Config.load(REAL_CONFIG)
    by_name = {s.name: s for s in sources.registry(config)}
    atlantic = by_name["hurdat2 (atlantic)"]
    assert atlantic.url == config.get("basins.atlantic.url")
    assert atlantic.sha256 == config.get("basins.atlantic.sha256")
    assert atlantic.target == config.path("basins.atlantic.path")


# -- rewriting the configuration --------------------------------------------

@pytest.fixture
def config_copy(tmp_path):
    target = tmp_path / "pipeline.yaml"
    shutil.copy(REAL_CONFIG, target)
    return target


def test_adopt_release_updates_exactly_the_four_source_keys(config_copy):
    before = config_copy.read_text().splitlines()
    release = _release()

    sources.adopt_release(
        config_copy, "atlantic", release,
        local_path=Path("data/raw/hurdat2/hurdat2-atl-1851-2099-03151700.txt"),
        checksum="f" * 64,
        today=dt.date(2100, 3, 20),
    )

    after = config_copy.read_text().splitlines()
    assert len(before) == len(after)
    changed = [line.strip() for old, line in zip(before, after) if old != line]
    assert changed == [
        "path: data/raw/hurdat2/hurdat2-atl-1851-2099-03151700.txt",
        f"url: {release.url}",
        "retrieved: 2100-03-20",
        f"sha256: {'f' * 64}",
    ]


def test_adopt_release_leaves_the_other_basin_untouched(config_copy):
    original = Config.load(config_copy)
    pacific_before = original.get("basins.pacific.sha256")

    sources.adopt_release(
        config_copy, "atlantic", _release(),
        local_path=Path("x.txt"), checksum="a" * 64,
    )

    assert Config.load(config_copy).get("basins.pacific.sha256") == pacific_before


def test_adopt_release_preserves_every_comment(config_copy):
    def comments(text):
        return [line for line in text.splitlines() if line.lstrip().startswith("#")]

    before = comments(config_copy.read_text())
    sources.adopt_release(
        config_copy, "pacific", _release(),
        local_path=Path("x.txt"), checksum="b" * 64,
    )
    assert comments(config_copy.read_text()) == before


def test_config_still_loads_and_enforces_provenance_after_a_rewrite(config_copy):
    sources.adopt_release(
        config_copy, "atlantic", _release(),
        local_path=Path("data/raw/hurdat2/new.txt"), checksum="c" * 64,
    )
    reloaded = Config.load(config_copy)
    # The calibration register must survive intact -- a rewrite that dropped a
    # status or source would make the pipeline refuse to run, or worse, run
    # on an unprovenanced value.
    assert reloaded.calibration("bypass_radius_km") == 111.12
    assert reloaded.calibrations["gate_spacing_km"].confirmed_by == "PI"
    assert reloaded.get("basins.atlantic.sha256") == "c" * 64


def test_adopt_release_refuses_an_unknown_basin(config_copy):
    with pytest.raises(KeyError, match="indian"):
        sources.adopt_release(
            config_copy, "indian", _release(),
            local_path=Path("x.txt"), checksum="d" * 64,
        )


def test_list_releases_rejects_an_unknown_basin():
    with pytest.raises(ValueError, match="unknown basin"):
        sources.list_releases("indian")
