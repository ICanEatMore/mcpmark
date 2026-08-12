"""Cross-process leases for logical environment slots and host ports."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterable


class LeaseUnavailableError(RuntimeError):
    """Raised when no requested logical resource can be leased."""


@dataclass
class SlotLease:
    """Ownership token for one logical environment slot."""

    slot_id: str
    token: str
    path: Path
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"cannot verify slot lease {self.slot_id}: {exc}"
            ) from exc
        if payload.get("token") != self.token:
            raise RuntimeError(f"slot lease ownership changed: {self.slot_id}")
        self.path.unlink()
        self._released = True

    def __enter__(self) -> "SlotLease":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class SlotLeasePool:
    """Filesystem-backed slot allocator shared by evaluator processes."""

    def __init__(self, state_dir: Path, slot_ids: Iterable[str]) -> None:
        self.state_dir = state_dir
        self.slot_ids = tuple(slot_ids)
        if not self.slot_ids:
            raise ValueError("at least one slot ID is required")
        if len(set(self.slot_ids)) != len(self.slot_ids):
            raise ValueError("slot IDs must be unique")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.state_dir / ".slots.lock"

    def acquire(self, owner: str) -> SlotLease:
        with self._locked():
            for slot_id in self.slot_ids:
                lease_path = self.state_dir / f"{slot_id}.lease.json"
                token = uuid.uuid4().hex
                payload = {
                    "slot_id": slot_id,
                    "owner": owner,
                    "token": token,
                    "pid": os.getpid(),
                    "created_at": time.time(),
                }
                try:
                    descriptor = os.open(
                        lease_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError:
                    continue
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, sort_keys=True)
                return SlotLease(slot_id=slot_id, token=token, path=lease_path)
        raise LeaseUnavailableError("no logical environment slot is available")

    def _locked(self) -> IO[str]:
        return _FileLock(self._lock_path)


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: IO[str] | None = None

    def __enter__(self) -> IO[str]:
        self._stream = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)
        return self._stream

    def __exit__(self, *_: object) -> None:
        assert self._stream is not None
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()


@dataclass
class PortLease:
    """A bound TCP socket that prevents allocation races until service start."""

    host: str
    port: int
    _socket: socket.socket | None

    def release(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def __enter__(self) -> "PortLease":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def reserve_tcp_port(host: str = "127.0.0.1") -> PortLease:
    """Reserve an ephemeral TCP port and hold it until the caller releases it."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        listener.bind((host, 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        return PortLease(host=host, port=port, _socket=listener)
    except Exception:
        listener.close()
        raise
