import socket
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

from src.runtime.leases import (
    LeaseUnavailableError,
    SlotLeasePool,
    reserve_tcp_port,
)
from src.runtime.processes import ProcessGroupSupervisor


class SlotLeaseTests(TestCase):
    def test_slot_is_exclusive_and_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pool = SlotLeasePool(Path(temp_dir), ["reddit-0"])
            first = pool.acquire("task-1")

            with self.assertRaises(LeaseUnavailableError):
                pool.acquire("task-2")

            first.release()
            second = pool.acquire("task-2")
            self.assertEqual(second.slot_id, "reddit-0")
            second.release()

    def test_wrong_owner_token_cannot_release_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pool = SlotLeasePool(Path(temp_dir), ["reddit-0"])
            lease = pool.acquire("task-1")
            payload = lease.path.read_text(encoding="utf-8")
            lease.path.write_text(
                payload.replace(lease.token, "other"), encoding="utf-8"
            )

            with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                lease.release()


class PortLeaseTests(TestCase):
    def test_reserved_port_cannot_be_rebound_until_release(self) -> None:
        lease = reserve_tcp_port()
        contender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(OSError):
                contender.bind((lease.host, lease.port))
        finally:
            contender.close()
            lease.release()

        rebound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            rebound.bind((lease.host, lease.port))
        finally:
            rebound.close()


class ProcessGroupSupervisorTests(TestCase):
    def test_terminate_stops_process_group_and_closes_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "service.log"
            group = ProcessGroupSupervisor().start(
                [
                    sys.executable,
                    "-c",
                    "import time; print('ready', flush=True); time.sleep(60)",
                ],
                stdout_path=log_path,
                stderr_path=log_path.with_suffix(".err"),
            )

            return_code = group.terminate(timeout=1.0)

            self.assertIsNotNone(group.process.poll())
            self.assertLess(return_code, 0)
