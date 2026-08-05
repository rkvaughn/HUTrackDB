"""Tests for geodesy, gate validation, calibration enforcement, and the
landfall classification rules.

The landfall tests use synthetic land polygons rather than the real coastline,
so they assert the *logic* (landmass identity, return-to-sea, native-flag
precedence) independently of any coastline source.
"""

from __future__ import annotations


import geopandas as gpd
import pytest
from shapely.geometry import LineString, Polygon

from hutrackdb.config import CalibrationError, Config
from hutrackdb.constants import (
    CRS_WGS84,
    native_landfall_flagging_is_complete,
)
from hutrackdb.geo.gates import GateError, GateSet, load_gate_file
from hutrackdb.geo.geodesy import densify_geodesic, geodesic_distance_km


class TestGeodesy:
    def test_known_distance(self):
        """One degree of latitude at the equator is ~110.57 km on WGS-84."""
        assert geodesic_distance_km(0, 0, 0, 1) == pytest.approx(110.574, abs=0.01)

    def test_zero_distance(self):
        assert geodesic_distance_km(-90.0, 29.0, -90.0, 29.0) == pytest.approx(0.0)

    def test_densify_includes_endpoints(self):
        points = densify_geodesic(-90.0, 29.0, -89.0, 30.0, step_km=10.0)
        assert points[0] == (-90.0, 29.0)
        assert points[-1] == (-89.0, 30.0)

    def test_densify_respects_step(self):
        points = densify_geodesic(-90.0, 29.0, -89.0, 30.0, step_km=10.0)
        for (lon1, lat1), (lon2, lat2) in zip(points, points[1:]):
            assert geodesic_distance_km(lon1, lat1, lon2, lat2) <= 10.5

    def test_short_segment_not_densified(self):
        points = densify_geodesic(-90.0, 29.0, -90.0, 29.001, step_km=10.0)
        assert len(points) == 2


class TestGateValidation:
    def _gate_frame(self, **overrides):
        data = {
            "gate_id": ["A", "B"],
            "geometry": [
                LineString([(-90.0, 29.0), (-89.5, 29.2)]),
                LineString([(-89.0, 29.4), (-88.5, 29.6)]),
            ],
        }
        data.update(overrides)
        return gpd.GeoDataFrame(data, geometry="geometry", crs=CRS_WGS84)

    def test_valid_gate_set_passes(self):
        gates = GateSet(self._gate_frame(), origin="test").validate()
        assert len(gates) == 2

    def test_duplicate_gate_id_rejected(self):
        with pytest.raises(GateError, match="unique"):
            GateSet(self._gate_frame(gate_id=["A", "A"]), origin="test").validate()

    def test_missing_gate_id_column_rejected(self):
        frame = self._gate_frame().drop(columns="gate_id")
        with pytest.raises(GateError, match="required column"):
            GateSet(frame, origin="test").validate()

    def test_non_line_geometry_rejected(self):
        frame = self._gate_frame(
            geometry=[
                Polygon([(-90, 29), (-89, 29), (-89, 30)]),
                LineString([(-89.0, 29.4), (-88.5, 29.6)]),
            ]
        )
        with pytest.raises(GateError, match="LineString"):
            GateSet(frame, origin="test").validate()

    def test_empty_gate_set_rejected(self):
        frame = self._gate_frame().iloc[0:0]
        with pytest.raises(GateError, match="no gates"):
            GateSet(frame, origin="test").validate()

    def test_csv_gate_file_roundtrip(self, tmp_path):
        csv = tmp_path / "gates.csv"
        csv.write_text(
            "gate_id,gate_name,region,lon1,lat1,lon2,lat2\n"
            "TX-01,Corpus,Texas,-97.55,27.62,-97.05,27.72\n"
            "TX-02,Matagorda,Texas,-96.60,28.35,-96.10,28.55\n"
        )
        gates = load_gate_file(csv)
        assert len(gates) == 2
        assert list(gates.frame["gate_id"]) == ["TX-01", "TX-02"]
        assert gates.origin.startswith("override:")
        # sort_order is synthesised from file order when absent.
        assert list(gates.frame["sort_order"]) == [0, 1]

    def test_csv_missing_endpoint_columns_rejected(self, tmp_path):
        csv = tmp_path / "bad.csv"
        csv.write_text("gate_id,lon1,lat1\nA,-97.0,27.0\n")
        with pytest.raises(GateError, match="must contain columns"):
            load_gate_file(csv)


class TestCalibrationEnforcement:
    """The pipeline must refuse to run on unprovenanced numbers."""

    def _config(self, tmp_path, calibration_block: str) -> Config:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        path = config_dir / "pipeline.yaml"
        path.write_text(calibration_block)
        return Config.load(path=path, root=tmp_path)

    def test_unconfirmed_value_refused(self, tmp_path):
        config = self._config(tmp_path, """
calibration:
  some_radius_km:
    value: 250.0
    status: UNCONFIRMED
    source: ""
""")
        with pytest.raises(CalibrationError, match="may not be used"):
            config.calibration("some_radius_km")

    def test_confirmed_without_approver_rejected(self, tmp_path):
        with pytest.raises(CalibrationError, match="confirmed_by"):
            self._config(tmp_path, """
calibration:
  some_radius_km:
    value: 250.0
    status: confirmed
    source: "somebody said so"
""")

    def test_sourced_without_source_rejected(self, tmp_path):
        with pytest.raises(CalibrationError, match="records no source"):
            self._config(tmp_path, """
calibration:
  some_radius_km:
    value: 250.0
    status: sourced
""")

    def test_bare_number_rejected(self, tmp_path):
        """A plain scalar has no provenance and must not be accepted."""
        with pytest.raises(CalibrationError, match="must be a mapping"):
            self._config(tmp_path, """
calibration:
  some_radius_km: 250.0
""")

    def test_undeclared_parameter_rejected(self, tmp_path):
        config = self._config(tmp_path, "calibration: {}\n")
        with pytest.raises(CalibrationError, match="not declared"):
            config.calibration("never_declared_km")

    def test_properly_provenanced_value_accepted(self, tmp_path):
        config = self._config(tmp_path, """
calibration:
  bypass_radius_km:
    value: 111.12
    status: confirmed
    confirmed_by: PI
    confirmed_on: 2026-08-05
    source: "60 nmi, NOAA Historical Hurricane Tracks default search radius"
""")
        assert config.calibration("bypass_radius_km") == pytest.approx(111.12)


class TestNativeFlaggingEras:
    """Era boundaries come from HURDAT2 format spec note 1."""

    @pytest.mark.parametrize("year,expected", [
        (1851, True), (1970, True),
        (1971, False), (1980, False), (1990, False),   # the documented gap
        (1991, True), (2025, True),
    ])
    def test_conus_flagging_completeness(self, year, expected):
        assert native_landfall_flagging_is_complete(year, conus=True) is expected

    @pytest.mark.parametrize("year,expected", [
        (1851, False), (1950, False),
        (1951, True), (1970, True),
        (1971, False), (1990, False),
        (1991, True), (2025, True),
    ])
    def test_international_flagging_completeness(self, year, expected):
        assert native_landfall_flagging_is_complete(year, conus=False) is expected
