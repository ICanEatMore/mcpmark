"""Bounded process-group lifecycle for task-local subprocess trees."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Mapping, Sequence


@dataclass
class ManagedProcessGroup:
    """One subprocess session whose descendants share a process group."""

    process: subprocess.Popen[str]
    stdout_stream: IO[str] | None = None
    stderr_stream: IO[str] | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    def terminate(self, timeout: float = 10.0) -> int:
        """TERM, bounded wait, then KILL the complete process group."""

        try:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=max(timeout, 1.0))
            return int(self.process.returncode or 0)
        finally:
            if self.stdout_stream is not None:
                self.stdout_stream.close()
                self.stdout_stream = None
            if self.stderr_stream is not None:
                self.stderr_stream.close()
                self.stderr_stream = None

    def __enter__(self) -> "ManagedProcessGroup":
        return self

    def __exit__(self, *_: object) -> None:
        self.terminate()


class ProcessGroupSupervisor:
    """Start foreground services in isolated POSIX sessions."""

    def start(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
    ) -> ManagedProcessGroup:
        if not command:
            raise ValueError("command must not be empty")

        stdout_stream = self._open_log(stdout_path)
        stderr_stream = self._open_log(stderr_path)
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=stdout_stream or subprocess.DEVNULL,
                stderr=stderr_stream or subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except Exception:
            if stdout_stream is not None:
                stdout_stream.close()
            if stderr_stream is not None:
                stderr_stream.close()
            raise
        return ManagedProcessGroup(process, stdout_stream, stderr_stream)

    @staticmethod
    def _open_log(path: Path | None) -> IO[str] | None:
        if path is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("a", encoding="utf-8")
