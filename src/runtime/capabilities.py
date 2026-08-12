"""Read-only capability checks for portable evaluation runtimes.

The checks in this module deliberately do not start containers, services, or
privileged runtimes. Launchers use the result to fail early with actionable
diagnostics instead of silently changing benchmark semantics.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from argparse import ArgumentParser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


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


def _command_capability(name: str, command: Sequence[str]) -> Capability:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Capability(name, False, f"{' '.join(command)} failed: {exc}")

    detail = (result.stderr or result.stdout).strip()
    if result.returncode == 0:
        return Capability(name, True, detail or "command succeeded")
    return Capability(
        name,
        False,
        detail or f"{' '.join(command)} exited with {result.returncode}",
    )


def _playwright_browser_capability() -> Capability:
    playwright = shutil.which("playwright")
    if not playwright:
        return Capability("playwright_chromium", False, "playwright CLI not found")

    try:
        result = subprocess.run(
            [playwright, "install", "--dry-run", "chromium"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Capability("playwright_chromium", False, f"dry-run failed: {exc}")

    if result.returncode != 0:
        return Capability(
            "playwright_chromium",
            False,
            (result.stderr or result.stdout).strip() or "playwright dry-run failed",
        )

    locations = re.findall(r"Install location:\s+(.+)", result.stdout)
    chromium_locations = [Path(value.strip()) for value in locations]
    for location in chromium_locations:
        if not location.is_dir():
            continue
        for root, _, files in os.walk(location):
            for filename in files:
                candidate = Path(root) / filename
                if os.access(candidate, os.X_OK) and filename in {
                    "chrome",
                    "headless_shell",
                }:
                    return Capability("playwright_chromium", True, str(candidate))

    detail = ", ".join(str(path) for path in chromium_locations) or "no install path"
    return Capability(
        "playwright_chromium", False, f"browser executable missing: {detail}"
    )


def _path_capability(name: str, path: Path, *, writable: bool = False) -> Capability:
    available = path.exists() and (not writable or os.access(path, os.W_OK))
    requirement = "exist and be writable" if writable else "exist"
    return Capability(name, available, f"{path} must {requirement}")


def _overlay_capability() -> Capability:
    filesystems = Path("/proc/filesystems")
    try:
        supported = "overlay" in filesystems.read_text().split()
    except OSError as exc:
        return Capability("overlayfs", False, f"cannot read {filesystems}: {exc}")
    return Capability("overlayfs", supported, "overlay listed in /proc/filesystems")


def _cni_capability() -> Capability:
    candidates = (
        Path("/usr/libexec/cni"),
        Path("/usr/local/libexec/cni"),
        Path("/opt/cni/bin"),
    )
    for directory in candidates:
        portmap = directory / "portmap"
        if portmap.is_file() and os.access(portmap, os.X_OK):
            return Capability("cni_portmap", True, str(portmap))
    return Capability(
        "cni_portmap",
        False,
        "executable CNI portmap plugin not found in standard paths",
    )


def probe_capabilities() -> list[Capability]:
    """Return the runtime prerequisites without altering host state.

    `apptainer` and `k3s` are intentionally reported independently. Their
    binaries being present is not proof that a workload can start: launchers
    must run their service-specific reset/health checks before evaluation.
    """

    capabilities = [
        _executable_capability("playwright"),
        _playwright_browser_capability(),
        _executable_capability("chromium", ("chromium-browser", "google-chrome")),
        _executable_capability("apptainer", ("singularity",)),
        _executable_capability("bwrap"),
        _executable_capability("postgres"),
        _executable_capability("k3s"),
        _executable_capability("docker"),
        _path_capability("fuse_device", Path("/dev/fuse"), writable=True),
        _overlay_capability(),
        _cni_capability(),
    ]

    user_namespace_path = Path("/proc/sys/kernel/unprivileged_userns_clone")
    if user_namespace_path.exists():
        capabilities.append(_kernel_toggle(user_namespace_path, "userns_sysctl"))
    else:
        capabilities.append(
            Capability(
                "userns_sysctl",
                True,
                "kernel toggle is not exposed; rely on user_namespace_smoke",
            )
        )

    unshare = shutil.which("unshare")
    if unshare:
        capabilities.append(
            _command_capability("user_namespace_smoke", [unshare, "-Ur", "true"])
        )
    else:
        capabilities.append(
            Capability("user_namespace_smoke", False, "unshare command not found")
        )

    try:
        filesystem_type = Path("/sys/fs/cgroup").stat().st_dev
        del (
            filesystem_type
        )  # Presence is useful even where statfs is unavailable in Python.
        controllers = Path("/sys/fs/cgroup/cgroup.controllers")
        if controllers.exists():
            capabilities.append(Capability("cgroup_v2", True, str(controllers)))
            subtree_control = Path("/sys/fs/cgroup/cgroup.subtree_control")
            capabilities.append(
                _path_capability("cgroup_delegation", subtree_control, writable=True)
            )
        else:
            capabilities.append(
                Capability("cgroup_v2", False, "cgroup v2 controller file not found")
            )
            capabilities.append(
                Capability("cgroup_delegation", False, "cgroup v2 is not available")
            )
    except OSError as exc:
        capabilities.append(
            Capability("cgroup_v2", False, f"cannot inspect cgroup: {exc}")
        )
        capabilities.append(
            Capability("cgroup_delegation", False, f"cannot inspect cgroup: {exc}")
        )

    return capabilities


def main(argv: Sequence[str] | None = None) -> int:
    """Print a machine-readable capability report for the launcher gate."""

    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="CAPABILITY",
        help="exit non-zero unless this named capability is available",
    )
    args = parser.parse_args(argv)

    capabilities = probe_capabilities()
    print(json.dumps([asdict(item) for item in capabilities], indent=2))
    by_name = {item.name: item for item in capabilities}
    return int(
        any(name not in by_name or not by_name[name].available for name in args.require)
    )


if __name__ == "__main__":
    raise SystemExit(main())
