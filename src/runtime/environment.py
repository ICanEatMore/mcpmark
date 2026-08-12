"""Runtime-neutral contracts for resettable evaluation environments.

The contracts describe lifecycle and evidence.  Concrete backends (for
example Apptainer) own the mechanics, while state managers continue to expose
the same task-facing URLs and metadata used by the benchmark.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from .capabilities import Capability


@dataclass(frozen=True)
class GoldenArtifact:
    """An immutable application artifact and the evidence identifying it."""

    path: Path
    digest: str
    source_digest: str
    architecture: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EnvironmentSpec:
    """Static requirements for one category of evaluation environment."""

    category: str
    artifact: GoldenArtifact
    internal_port: int
    readiness_path: str = "/"
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EnvironmentHandle:
    """A leased logical slot containing only task-visible runtime metadata."""

    run_id: str
    slot_id: str
    category: str
    base_url: str
    artifact_digest: str
    state_fingerprint: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class RuntimeBackend(ABC):
    """Lifecycle contract implemented by an environment runtime backend.

    Implementations must make ``reset`` idempotent and must not return from
    ``verify`` successfully unless the slot matches its golden fingerprint.
    ``release_slot`` must be safe to call after a partial setup failure.
    """

    @abstractmethod
    def doctor(self) -> Sequence["Capability"]:
        """Report backend prerequisites without changing benchmark state."""

    @abstractmethod
    def materialize(self, spec: EnvironmentSpec) -> GoldenArtifact:
        """Validate or build the immutable artifact declared by ``spec``."""

    @abstractmethod
    def acquire_slot(self, spec: EnvironmentSpec) -> EnvironmentHandle:
        """Lease an isolated logical slot for one task."""

    @abstractmethod
    def reset(self, handle: EnvironmentHandle) -> EnvironmentHandle:
        """Restore a slot to the immutable golden state."""

    @abstractmethod
    def start(self, handle: EnvironmentHandle) -> EnvironmentHandle:
        """Start the slot's long-running application processes."""

    @abstractmethod
    def execute(
        self, handle: EnvironmentHandle, command: Sequence[str]
    ) -> tuple[int, str, str]:
        """Run an administrative command inside the environment."""

    @abstractmethod
    def verify(self, handle: EnvironmentHandle) -> EnvironmentHandle:
        """Verify readiness and the golden-state fingerprint."""

    @abstractmethod
    def stop(self, handle: EnvironmentHandle) -> None:
        """Stop every process belonging to the slot."""

    @abstractmethod
    def release_slot(self, handle: EnvironmentHandle) -> None:
        """Release the slot and discard all task-writable state."""


class EnvironmentSession:
    """Orchestrate one verified environment slot with bounded cleanup.

    A session is usable only after reset, start, and verification have all
    succeeded.  Any exception after slot acquisition triggers best-effort stop
    followed by release; cleanup exceptions never replace the original setup
    exception.
    """

    def __init__(self, backend: RuntimeBackend, spec: EnvironmentSpec) -> None:
        self.backend = backend
        self.spec = spec
        self.handle: EnvironmentHandle | None = None
        self._released = False

    def prepare(self) -> EnvironmentHandle:
        if self.handle is not None:
            raise RuntimeError("environment session is already prepared")

        artifact = self.backend.materialize(self.spec)
        if artifact.digest != self.spec.artifact.digest:
            raise RuntimeError(
                "materialized artifact digest does not match EnvironmentSpec"
            )

        self.handle = self.backend.acquire_slot(self.spec)
        try:
            self.handle = self.backend.reset(self.handle)
            self.handle = self.backend.start(self.handle)
            self.handle = self.backend.verify(self.handle)
        except BaseException:
            self.close(suppress_errors=True)
            raise

        if self.handle.artifact_digest != artifact.digest:
            self.close(suppress_errors=True)
            raise RuntimeError("verified slot does not match golden artifact digest")
        if not self.handle.state_fingerprint:
            self.close(suppress_errors=True)
            raise RuntimeError("verified slot has no state fingerprint")
        return self.handle

    def close(self, *, suppress_errors: bool = False) -> None:
        if self.handle is None or self._released:
            return

        errors: list[BaseException] = []
        try:
            self.backend.stop(self.handle)
        except BaseException as exc:
            errors.append(exc)
        try:
            self.backend.release_slot(self.handle)
        except BaseException as exc:
            errors.append(exc)
        else:
            self._released = True

        if errors and not suppress_errors:
            raise RuntimeError("environment cleanup failed") from errors[0]

    def __enter__(self) -> EnvironmentHandle:
        return self.prepare()

    def __exit__(self, *_: object) -> None:
        self.close()
