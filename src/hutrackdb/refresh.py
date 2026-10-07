"""One-command refresh onto the current HURDAT2 release.

HURDAT2 is revised once a year, in late winter or spring, after NHC finishes
post-season best-track analysis. Adopting a revision used to mean editing a
filename, a URL and a checksum by hand in two files that had to agree. This
module reduces that to:

    hutrackdb refresh --check     # is a newer release available?
    hutrackdb refresh             # adopt it, rebuild, and re-validate

The checksum question is worth being explicit about. A pinned SHA-256 exists to
prove the file you build against today is the file you built against before --
it cannot authenticate a release you have never seen. So on adoption the
checksum is COMPUTED from the download and recorded, and from then on it is
enforced. `--check` never writes anything.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from . import sources
from .config import Config

log = logging.getLogger(__name__)


def check(config: Config) -> list[tuple[str, sources.Release | None, bool]]:
    """Compare each enabled basin against the newest published release."""
    findings = []
    for basin in sources.enabled_basins(config):
        current_url = sources.configured_url(config, basin)
        try:
            newest = sources.latest_release(basin)
        except Exception as exc:                      # noqa: BLE001
            log.warning("could not reach the NHC listing for %s: %s", basin, exc)
            findings.append((basin, None, False))
            continue
        is_current = newest is not None and newest.url == current_url
        findings.append((basin, newest, is_current))
    return findings


def render_check(config: Config, findings) -> str:
    lines = ["HURDAT2 RELEASE CHECK", ""]
    for basin, newest, is_current in findings:
        current_url = sources.configured_url(config, basin) or "(none)"
        lines.append(f"  {basin}")
        lines.append(f"    in use : {Path(current_url).name}")
        if newest is None:
            lines.append("    latest : could not be determined (no network?)")
        elif is_current:
            lines.append(f"    latest : {newest.describe()}")
            lines.append("    status : UP TO DATE")
        else:
            lines.append(f"    latest : {newest.describe()}")
            lines.append("    status : NEWER RELEASE AVAILABLE")
        lines.append("")

    stale = [b for b, n, ok in findings if n is not None and not ok]
    if stale:
        lines += [
            f"A newer release is available for: {', '.join(stale)}.",
            "Run `hutrackdb refresh` to adopt it, rebuild, and re-validate.",
        ]
    else:
        lines.append("Everything is current. HURDAT2 is revised once a year, "
                     "usually February-May, so checking each spring is enough.")
    return "\n".join(lines)


def refresh(config: Config, *, dry_run: bool = False,
            notebook: bool = True, animation: bool = True) -> int:
    """Adopt the newest release for every enabled basin, then rebuild."""
    findings = check(config)
    print(render_check(config, findings))
    print()

    stale = [(basin, newest) for basin, newest, ok in findings
             if newest is not None and not ok]
    unreachable = [b for b, n, _ in findings if n is None]
    if unreachable:
        print(f"Cannot proceed: the NHC listing was unreachable for "
              f"{', '.join(unreachable)}. Check your connection and retry.")
        return 1
    if not stale:
        print("Nothing to do.")
        return 0
    if dry_run:
        print("--check was given, so nothing was downloaded or changed.")
        return 0

    raw_dir = config.root / "data" / "raw" / "hurdat2"
    for basin, release in stale:
        print(f"[{basin}] downloading {release.filename} ...")
        # Keep the basin in the local filename so the two are never confusable
        # on disk, even though NHC's Atlantic names omit it.
        local_name = release.filename
        if basin == "atlantic" and "-atl-" not in local_name:
            local_name = local_name.replace("hurdat2-", "hurdat2-atl-", 1)
        target = raw_dir / local_name
        sources.download(release.url, target)
        checksum = sources.sha256_of(target)
        print(f"[{basin}] {target.stat().st_size:,} bytes, sha256 {checksum[:16]}...")

        sources.adopt_release(
            config.config_path, basin, release,
            local_path=target.relative_to(config.root),
            checksum=checksum,
        )
        print(f"[{basin}] config/pipeline.yaml updated")

    print()
    print("Sources updated. Rebuilding ...")
    print(flush=True)

    # Reload: the configuration on disk has changed underneath us.
    config = Config.load(config.config_path, root=config.root)

    from .db.snowflake import write_snowflake_ddl
    from .db.writers import write_all
    from .pipeline import run

    result = run(config)
    write_all(result, config)
    write_snowflake_ddl(config)
    print(result.summary())

    # Everything downstream of the tables. Each step reports its own outcome;
    # a failure here does not invalidate the rebuild, so the run continues and
    # the summary at the end says plainly what did and did not succeed.
    outcomes = [("rebuild the database", True)]
    outcomes.append(("re-run the QA validation", _step_qa(config)))
    if notebook:
        outcomes.append(("re-execute the notebook", _step_notebook(config)))
    if animation:
        outcomes.append(("regenerate the README animation", _step_animation(config)))
    outcomes.append(("check the counts quoted in the docs", _step_doc_counts(config)))

    print()
    print("=" * 70)
    print("REFRESH COMPLETE")
    print("=" * 70)
    for label, ok in outcomes:
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    print()

    if all(ok for _, ok in outcomes):
        print("Everything is consistent. Two things are left, and both need a human:")
    else:
        print("Some steps did not succeed — see their output above. Once resolved:")
    print()
    print("  1. Read data/processed/qa_report.md rather than just noting it ran.")
    print("     A large swing against the All U.S. Hurricanes list would mean the")
    print("     new release changed landfall semantics, not just added a season.")
    print()
    print("  2. Commit the regenerated data/processed/parquet/ together with the")
    print("     config change, so the committed tables and the pipeline that")
    print("     produced them never disagree.")
    return 0 if all(ok for _, ok in outcomes) else 1


def _run(label: str, command: list[str], cwd: Path) -> bool:
    print()
    print("-" * 70)
    print(f"{label}: {' '.join(command)}")
    print("-" * 70)
    # Children write straight to our stdout. When that stdout is a pipe or a
    # log file rather than a terminal it is block-buffered, so without this
    # flush every heading printed here would surface AFTER all the child
    # output -- turning a saved refresh log into nonsense.
    sys.stdout.flush()
    try:
        completed = subprocess.run(command, cwd=cwd, check=False)
    except FileNotFoundError as exc:                  # noqa: BLE001
        print(f"  could not run: {exc}")
        return False
    return completed.returncode == 0


def _step_qa(config: Config) -> bool:
    from .qa.validate import run_qa

    print()
    print("-" * 70)
    print("QA validation")
    print("-" * 70)
    try:
        report = run_qa(config)
    except Exception as exc:                          # noqa: BLE001
        print(f"  QA could not run: {exc}")
        return False
    (config.output_dir() / "qa_report.md").write_text(report.render())
    print(report.render())
    return report.passed


def _step_notebook(config: Config) -> bool:
    notebook = config.root / "notebooks" / "eda_validation.ipynb"
    if not notebook.exists():
        print(f"\nno notebook at {notebook}; skipping")
        return True
    ok = _run("Notebook", [
        sys.executable, "-m", "nbconvert", "--to", "notebook",
        "--execute", "--inplace", str(notebook.relative_to(config.root)),
    ], cwd=config.root)
    if not ok:
        print("  The notebook did not execute. If this is ModuleNotFoundError,")
        print("  install the extras:  pip install -e \".[all]\"")
    return ok


def _step_animation(config: Config) -> bool:
    script = config.root / "scripts" / "animate_landfalls.py"
    if not script.exists():
        return True
    ok = _run("Animation", [
        sys.executable, str(script.relative_to(config.root)), "--preset", "share",
    ], cwd=config.root)
    if not ok:
        print("  The animation did not render. If this is ModuleNotFoundError,")
        print("  install the extras:  pip install -e \".[all]\"")
    return ok


def _step_doc_counts(config: Config) -> bool:
    """Counts quoted in the README and docs are hardcoded and go stale here."""
    script = config.root / "scripts" / "check_doc_counts.py"
    if not script.exists():
        return True
    ok = _run("Documented counts", [
        sys.executable, str(script.relative_to(config.root)),
    ], cwd=config.root)
    if not ok:
        print("  The figures above are quoted in prose and must be edited by hand.")
        print("  CLAUDE.md lists which document quotes which number.")
    return ok
