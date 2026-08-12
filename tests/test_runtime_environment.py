from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from src.runtime.capabilities import Capability, main
from src.runtime.environment import (
    EnvironmentHandle,
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


class CapabilityCliTests(TestCase):
    @patch("src.runtime.capabilities.probe_capabilities")
    def test_required_capability_controls_exit_status(self, probe) -> None:
        probe.return_value = [Capability("apptainer", False, "missing")]

        self.assertEqual(main(["--require", "apptainer"]), 1)
        self.assertEqual(main([]), 0)
