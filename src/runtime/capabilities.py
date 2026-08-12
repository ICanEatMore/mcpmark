"""Read-only capability checks for portable evaluation runtimes.

The checks in this module deliberately do not start containers, services, or
privileged runtimes. Launchers use the result to fail early with actionable
diagnostics instead of silently changing benchmark semantics.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Capability:
    """One runtime prerequisite and its observed availability."""

    name: str
    available: bool
    detail: str


def _executable_capability(name: str, alternatives: Iterable[str] = ()) -> Capability:
    candidates = (name, *alternatives)
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return Capability(name, True, path)
    return Capability(name, False, f"not found (looked for: {', '.join(candidates)})")


def _kernel_toggle(path: Path, name: str) -> Capability:
    try:
        value = path.read_text().strip()
    except OSError as exc:
        return Capability(name, False, f"cannot read {path}: {exc}")
    return Capability(name, value == "1", f"{path}={value}")


def probe_capabilities() -> list[Capability]:
    """Return the runtime prerequisites without altering host state.

    `apptainer` and `k3s` are intentionally reported independently. Their
    binaries being present is not proof that a workload can start: launchers
    must run their service-specific reset/health checks before evaluation.
    """

    capabilities = [
        _executable_capability("playwright"),
        _executable_capability("chromium", ("chromium-browser", "google-chrome")),
        _executable_capability("apptainer", ("singularity",)),
        _executable_capability("bwrap"),
        _executable_capability("postgres"),
        _executable_capability("k3s"),
        _executable_capability("docker"),
    ]

    user_namespace_path = Path("/proc/sys/kernel/unprivileged_userns_clone")
    if user_namespace_path.exists():
        capabilities.append(_kernel_toggle(user_namespace_path, "unprivileged_userns"))
    else:
        capabilities.append(
            Capability(
                "unprivileged_userns",
                False,
                "kernel toggle is unavailable; verify user namespaces with the platform administrator",
            )
        )

    try:
        filesystem_type = Path("/sys/fs/cgroup").stat().st_dev
        del filesystem_type  # Presence is useful even where statfs is unavailable in Python.
        controllers = Path("/sys/fs/cgroup/cgroup.controllers")
        if controllers.exists():
            capabilities.append(Capability("cgroup_v2", True, str(controllers)))
        else:
            capabilities.append(Capability("cgroup_v2", False, "cgroup v2 controller file not found"))
    except OSError as exc:
        capabilities.append(Capability("cgroup_v2", False, f"cannot inspect cgroup: {exc}"))

    return capabilities


def main() -> int:
    """Print a machine-readable capability report for the launcher gate."""

    print(json.dumps([asdict(item) for item in probe_capabilities()], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
