"""Tests for the HURDAT2 fixed-format parser.

The example records used here are quoted from the HURDAT2 format specification
(Landsea, April 2022) so the expected values are traceable to the spec rather
than to the parser's own behaviour.
"""

from __future__ import annotations

import datetime as dt

import pytest

from hutrackdb.constants import saffir_simpson_category
from hutrackdb.parse.hurdat2 import Hurdat2ParseError, parse_file

# Hurricane Ida (2021) header plus the landfall record, verbatim from the
# format specification's worked example.
IDA_SAMPLE = """\
AL092021,                IDA,     4,
20210829, 1200,  , HU, 28.5N,  89.6W, 130,  929, 130, 110,  80, 110,  70,  60,\
  40,  60,  45,  35,  20,  30,  10
20210829, 1655, L, HU, 29.1N,  90.2W, 130,  931, 130, 110,  80, 110,  70,  60,\
  40,  60,  45,  35,  20,  30,  10
20210829, 1800,  , HU, 29.2N,  90.4W, 125,  932, 130, 120,  80,  80,  70,  60,\
  40,  40,  45,  35,  20,  25,  10
20210830, 0000,  , HU, 29.9N,  90.6W, 105,  944,  80, 120,  80,  70,  50,  60,\
  40,  40,  30,  30,  20,  20,  10
"""

# Pre-2021 line: 20 fields, no Radius of Maximum Wind.
LEGACY_SAMPLE = """\
AL011851,            UNNAMED,     2,
18510625, 0000,  , HU, 28.0N,  94.8W,  80, -999, -999, -999, -999, -999, -999,\
 -999, -999, -999, -999, -999, -999, -999
18510625, 0600,  , HU, 28.0N,  95.4W,  80, -999, -999, -999, -999, -999, -999,\
 -999, -999, -999, -999, -999, -999, -999
"""


def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    return path


class TestHeaderParsing:
    def test_identifier_decomposition(self, tmp_path):
        result = parse_file(write(tmp_path, "ida.txt", IDA_SAMPLE))
        storm = result.storms[0]
        assert storm.storm_id == "AL092021"
        assert storm.basin == "AL"
        assert storm.cyclone_number == 9
        assert storm.season == 2021
        assert storm.name == "IDA"
        assert storm.n_track_points == 4

    def test_unnamed_storms_preserved(self, tmp_path):
        result = parse_file(write(tmp_path, "legacy.txt", LEGACY_SAMPLE))
        assert result.storms[0].name == "UNNAMED"

    def test_declared_row_count_is_enforced(self, tmp_path):
        # Header claims 4 rows but only 2 follow.
        broken = IDA_SAMPLE.replace("     4,", "     9,")
        with pytest.raises(Hurdat2ParseError, match="still expected"):
            parse_file(write(tmp_path, "short.txt", broken))

    def test_unknown_basin_rejected(self, tmp_path):
        broken = IDA_SAMPLE.replace("AL092021", "ZZ092021")
        with pytest.raises(Hurdat2ParseError, match="unknown basin"):
            parse_file(write(tmp_path, "basin.txt", broken))

    def test_duplicate_storm_id_rejected(self, tmp_path):
        with pytest.raises(Hurdat2ParseError, match="duplicate"):
            parse_file(write(tmp_path, "dup.txt", IDA_SAMPLE + IDA_SAMPLE))


class TestDataLineParsing:
    def test_field_values(self, tmp_path):
        result = parse_file(write(tmp_path, "ida.txt", IDA_SAMPLE))
        landfall = result.track_points[1]
        assert landfall.record_identifier == "L"
        assert landfall.status == "HU"
        assert landfall.max_wind_kt == 130
        assert landfall.min_pressure_mb == 931
        assert landfall.radius_max_wind_nm == 10
        assert landfall.r34_ne_nm == 130

    def test_hemisphere_signs(self, tmp_path):
        result = parse_file(write(tmp_path, "ida.txt", IDA_SAMPLE))
        point = result.track_points[1]
        assert point.latitude == pytest.approx(29.1)
        # West longitude must be negative.
        assert point.longitude == pytest.approx(-90.2)

    def test_timestamps_are_utc(self, tmp_path):
        result = parse_file(write(tmp_path, "ida.txt", IDA_SAMPLE))
        point = result.track_points[1]
        assert point.timestamp == dt.datetime(2021, 8, 29, 16, 55, tzinfo=dt.timezone.utc)

    def test_asynoptic_detection(self, tmp_path):
        result = parse_file(write(tmp_path, "ida.txt", IDA_SAMPLE))
        # 1200 and 1800 are synoptic; the 1655 landfall record is not.
        assert result.track_points[0].is_synoptic is True
        assert result.track_points[1].is_synoptic is False
        assert result.track_points[2].is_synoptic is True

    def test_missing_sentinels_become_none(self, tmp_path):
        """-999 must never reach the database as a number."""
        result = parse_file(write(tmp_path, "legacy.txt", LEGACY_SAMPLE))
        point = result.track_points[0]
        assert point.min_pressure_mb is None
        assert point.r34_ne_nm is None
        assert point.radius_max_wind_nm is None
        assert point.max_wind_kt == 80  # a real value is preserved

    def test_legacy_20_field_lines_accepted(self, tmp_path):
        result = parse_file(write(tmp_path, "legacy.txt", LEGACY_SAMPLE))
        assert len(result.track_points) == 2
        assert all(p.radius_max_wind_nm is None for p in result.track_points)

    def test_unknown_status_rejected(self, tmp_path):
        broken = IDA_SAMPLE.replace(", HU, 28.5N", ", XX, 28.5N")
        with pytest.raises(Hurdat2ParseError, match="unknown status"):
            parse_file(write(tmp_path, "status.txt", broken))

    def test_unknown_record_identifier_rejected(self, tmp_path):
        broken = IDA_SAMPLE.replace("1655, L,", "1655, Q,")
        with pytest.raises(Hurdat2ParseError, match="unknown record identifier"):
            parse_file(write(tmp_path, "recid.txt", broken))

    def test_invalid_date_rejected(self, tmp_path):
        broken = IDA_SAMPLE.replace("20210829, 1200", "20210massive, 1200")
        with pytest.raises(Hurdat2ParseError):
            parse_file(write(tmp_path, "date.txt", broken))


class TestProvenance:
    def test_checksum_recorded(self, tmp_path):
        result = parse_file(write(tmp_path, "ida.txt", IDA_SAMPLE))
        assert len(result.source_sha256) == 64
        assert result.parsed_at_utc


class TestSaffirSimpson:
    """Thresholds are quoted from NHC; these lock the boundaries in place."""

    @pytest.mark.parametrize(
        "wind,expected",
        [
            (0, "TD"), (33, "TD"),
            (34, "TS"), (63, "TS"),
            (64, "1"), (82, "1"),
            (83, "2"), (95, "2"),
            (96, "3"), (112, "3"),
            (113, "4"), (136, "4"),
            (137, "5"), (200, "5"),
        ],
    )
    def test_category_boundaries(self, wind, expected):
        assert saffir_simpson_category(wind) == expected

    def test_missing_wind_is_unknown(self):
        assert saffir_simpson_category(None) == "UNK"
        assert saffir_simpson_category(-999) == "UNK"
        assert saffir_simpson_category(-99) == "UNK"
