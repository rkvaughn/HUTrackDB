"""Configuration loading with calibration-provenance enforcement.

The project forbids fabricated quantitative values. This module is the
mechanism that enforces it: every tunable number must be declared in
``config/pipeline.yaml`` with a ``status`` and a ``source``, and the pipeline
refuses to start if a parameter it actually uses is still ``UNCONFIRMED``.

The failure is loud and early, at config load, rather than at the point of use
-- so a run either has a fully-provenanced parameter set or does not happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Statuses that permit a parameter to be used in a pipeline run.
USABLE_STATUSES = frozenset({"sourced", "confirmed"})

#: Status marking a parameter as still requiring PI approval.
UNCONFIRMED_STATUS = "UNCONFIRMED"


class CalibrationError(RuntimeError):
    """Raised when a calibration parameter lacks approved provenance."""


@dataclass(frozen=True, slots=True)
class Calibration:
    """A single provenanced numeric parameter."""

    name: str
    value: float
    status: str
    source: str
    confirmed_by: str | None = None
    confirmed_on: str | None = None

    def require_usable(self) -> float:
        """Return the value, or raise if it is not approved for use."""
        if self.status not in USABLE_STATUSES:
            raise CalibrationError(
                f"Calibration parameter {self.name!r} has status {self.status!r} "
                f"and may not be used.\n"
                f"This project forbids fabricated quantitative values. Set a value in "
                f"config/pipeline.yaml, record its source, and change status to "
                f"'sourced' (external authority) or 'confirmed' (explicit PI approval, "
                f"with confirmed_by and confirmed_on)."
            )
        return self.value


class Config:
    """Parsed pipeline configuration rooted at the project directory."""

    def __init__(self, raw: dict[str, Any], root: Path, config_path: Path):
        self._raw = raw
        self.root = root
        self.config_path = config_path
        self._calibrations = self._load_calibrations(raw.get("calibration", {}))

    # -- construction -------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None, root: str | Path | None = None) -> "Config":
        """Load configuration. Defaults to ``<root>/config/pipeline.yaml``."""
        root_path = Path(root).resolve() if root else _find_project_root()
        config_path = Path(path) if path else root_path / "config" / "pipeline.yaml"
        if not config_path.exists():
            raise FileNotFoundError(f"configuration not found: {config_path}")
        with config_path.open() as handle:
            raw = yaml.safe_load(handle) or {}
        return cls(raw, root_path, config_path)

    @staticmethod
    def _load_calibrations(block: dict[str, Any]) -> dict[str, Calibration]:
        calibrations: dict[str, Calibration] = {}
        for name, entry in block.items():
            if not isinstance(entry, dict) or "value" not in entry:
                raise CalibrationError(
                    f"Calibration {name!r} must be a mapping with 'value', 'status' and "
                    f"'source' keys. A bare number is not permitted -- every quantitative "
                    f"value requires recorded provenance."
                )
            status = str(entry.get("status", UNCONFIRMED_STATUS))
            source = str(entry.get("source", "")).strip()
            if status in USABLE_STATUSES and not source:
                raise CalibrationError(
                    f"Calibration {name!r} is marked {status!r} but records no source."
                )
            if status == "confirmed" and not entry.get("confirmed_by"):
                raise CalibrationError(
                    f"Calibration {name!r} is marked 'confirmed' but records no "
                    f"confirmed_by. An approval must name its approver."
                )
            calibrations[name] = Calibration(
                name=name,
                value=float(entry["value"]),
                status=status,
                source=source,
                confirmed_by=entry.get("confirmed_by"),
                confirmed_on=str(entry["confirmed_on"]) if entry.get("confirmed_on") else None,
            )
        return calibrations

    # -- accessors ----------------------------------------------------------

    def calibration(self, name: str) -> float:
        """Return an approved calibration value, or raise :class:`CalibrationError`."""
        if name not in self._calibrations:
            raise CalibrationError(
                f"Calibration parameter {name!r} is not declared in {self.config_path}. "
                f"Undeclared numeric parameters are not permitted."
            )
        return self._calibrations[name].require_usable()

    @property
    def calibrations(self) -> dict[str, Calibration]:
        """All declared calibrations, for the provenance report."""
        return dict(self._calibrations)

    def section(self, name: str) -> dict[str, Any]:
        return dict(self._raw.get(name, {}) or {})

    def get(self, dotted: str, default: Any = None) -> Any:
        """Fetch a nested value, e.g. ``config.get("coastline.override_path")``."""
        node: Any = self._raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str, default: Any = None) -> Path | None:
        """Fetch a config value and resolve it against the project root."""
        value = self.get(dotted, default)
        if value in (None, ""):
            return None
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (self.root / candidate)

    def enabled_basin_paths(self) -> list[Path]:
        """Resolved paths of every basin marked enabled."""
        paths: list[Path] = []
        for name, entry in (self._raw.get("basins") or {}).items():
            if not (entry or {}).get("enabled"):
                continue
            basin_path = self.path(f"basins.{name}.path")
            if basin_path is None:
                raise FileNotFoundError(f"basin {name!r} is enabled but declares no path")
            if not basin_path.exists():
                raise FileNotFoundError(
                    f"basin {name!r} source not found: {basin_path}\n"
                    f"Run scripts/fetch_sources.py to download it."
                )
            paths.append(basin_path)
        if not paths:
            raise ValueError("no basins are enabled in the configuration")
        return paths

    def output_dir(self) -> Path:
        directory = self.path("output.dir", "data/processed")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def provenance_report(self) -> str:
        """Human-readable register of every calibration and its provenance."""
        lines = [
            "CALIBRATION PROVENANCE REGISTER",
            f"source: {self.config_path}",
            "",
        ]
        for name, cal in sorted(self._calibrations.items()):
            approval = ""
            if cal.confirmed_by:
                approval = f" (confirmed by {cal.confirmed_by} on {cal.confirmed_on})"
            lines.append(f"  {name} = {cal.value}  [{cal.status}]{approval}")
            source_text = " ".join(cal.source.split())
            lines.append(f"      source: {source_text}")
            lines.append("")
        return "\n".join(lines)


def _find_project_root() -> Path:
    """Walk upward from this file until a directory containing config/ is found."""
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config" / "pipeline.yaml").exists():
            return candidate
    raise FileNotFoundError(
        "could not locate project root (no config/pipeline.yaml found in any parent)"
    )
