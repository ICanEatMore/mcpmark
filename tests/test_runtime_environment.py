from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, call, patch

from src.runtime.capabilities import (
    Capability,
    _cni_capability,
    _fuse_capability,
    main,
)
from src.runtime.environment import (
    EnvironmentHandle,
    EnvironmentSession,
    EnvironmentSpec,
    GoldenArtifact,
    RuntimeBackend,
)


class RuntimeContractTests(TestCase):
    def test_runtime_backend_cannot_be_instantiated(self) -> None:
        with self.assertRaises(TypeError):
            RuntimeBackend()

    def test_environment_records_keep_golden_identity(self) -> None:
        artifact = GoldenArtifact(
            path=Path("/artifacts/reddit.sif"),
            digest="sha256:sif",
            source_digest="sha256:oci",
            architecture="amd64",
        )
        spec = EnvironmentSpec(category="reddit", artifact=artifact, internal_port=80)
        handle = EnvironmentHandle(
            run_id="run-1",
            slot_id="reddit-0",
            category=spec.category,
            base_url="http://127.0.0.1:9999",
            artifact_digest=spec.artifact.digest,
            state_fingerprint="sha256:state",
        )

        self.assertEqual(handle.artifact_digest, artifact.digest)
        self.assertEqual(handle.category, spec.category)

    def test_session_runs_verified_lifecycle_and_cleanup(self) -> None:
        backend, spec, handle = self._backend_fixture()

        with EnvironmentSession(backend, spec) as prepared:
            self.assertIs(prepared, handle)

        self.assertEqual(
            backend.method_calls,
            [
                call.materialize(spec),
                call.acquire_slot(spec),
                call.reset(handle),
                call.start(handle),
                call.verify(handle),
                call.stop(handle),
                call.release_slot(handle),
            ],
        )

    def test_session_releases_slot_when_verify_fails(self) -> None:
        backend, spec, handle = self._backend_fixture()
        backend.verify.side_effect = RuntimeError("fingerprint mismatch")

        with self.assertRaisesRegex(RuntimeError, "fingerprint mismatch"):
            EnvironmentSession(backend, spec).prepare()

        backend.stop.assert_called_once_with(handle)
        backend.release_slot.assert_called_once_with(handle)

    def test_session_rejects_missing_fingerprint(self) -> None:
        backend, spec, handle = self._backend_fixture(state_fingerprint="")

        with self.assertRaisesRegex(RuntimeError, "no state fingerprint"):
            EnvironmentSession(backend, spec).prepare()

        backend.release_slot.assert_called_once_with(handle)

    @staticmethod
    def _backend_fixture(state_fingerprint: str = "sha256:state"):
        artifact = GoldenArtifact(
            path=Path("/artifacts/reddit.sif"),
            digest="sha256:sif",
            source_digest="sha256:oci",
            architecture="amd64",
        )
        spec = EnvironmentSpec("reddit", artifact, 80)
        handle = EnvironmentHandle(
            run_id="run-1",
            slot_id="reddit-0",
            category="reddit",
            base_url="http://127.0.0.1:9999",
            artifact_digest=artifact.digest,
            state_fingerprint=state_fingerprint,
        )
        backend = Mock(spec=RuntimeBackend)
        backend.materialize.return_value = artifact
        backend.acquire_slot.return_value = handle
        backend.reset.return_value = handle
        backend.start.return_value = handle
        backend.verify.return_value = handle
        return backend, spec, handle


class CapabilityCliTests(TestCase):
    @patch("src.runtime.capabilities.probe_capabilities")
    def test_required_capability_controls_exit_status(self, probe) -> None:
        probe.return_value = [Capability("apptainer", False, "missing")]

        self.assertEqual(main(["--require", "apptainer"]), 1)
        self.assertEqual(main([]), 0)

    @patch("src.runtime.capabilities.os.open", side_effect=PermissionError("denied"))
    @patch("src.runtime.capabilities.Path.exists", return_value=True)
    def test_fuse_requires_device_open_not_only_path_presence(
        self, _exists, _open
    ) -> None:
        result = _fuse_capability()

        self.assertFalse(result.available)
        self.assertIn("cannot open", result.detail)

    @patch("src.runtime.capabilities.os.access", return_value=True)
    @patch("src.runtime.capabilities.Path.is_file", return_value=True)
    def test_cni_detects_apptainer_packaged_portmap(self, _is_file, _access) -> None:
        result = _cni_capability()

        self.assertTrue(result.available)
        self.assertIn("/usr/libexec/apptainer/cni/portmap", result.detail)
