import hashlib
import io
import json
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from src.runtime.artifacts import (
    ArtifactInventoryError,
    inspect_docker_save_archive,
)


class OciArchiveInventoryTests(TestCase):
    def test_inventory_records_source_and_image_identity(self) -> None:
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "postmill.tar"
            self._write_archive(archive_path)

            inventory = inspect_docker_save_archive(archive_path)

            self.assertEqual(inventory.schema_version, 1)
            self.assertEqual(inventory.source_filename, "postmill.tar")
            self.assertEqual(
                inventory.source_sha256,
                hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(inventory.config_digest, "a" * 64)
            self.assertEqual(inventory.repo_tags, ("postmill:golden",))
            self.assertEqual(inventory.operating_system, "linux")
            self.assertEqual(inventory.architecture, "amd64")
            self.assertEqual(inventory.layer_count, 2)

    def test_inventory_rejects_multiple_images(self) -> None:
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "ambiguous.tar"
            self._write_archive(archive_path, image_count=2)

            with self.assertRaisesRegex(
                ArtifactInventoryError, "exactly one image manifest"
            ):
                inspect_docker_save_archive(archive_path)

    @staticmethod
    def _write_archive(path: Path, image_count: int = 1) -> None:
        config_name = f"{'a' * 64}.json"
        image = {
            "Config": config_name,
            "RepoTags": ["postmill:golden"],
            "Layers": ["layer-1/layer.tar", "layer-2/layer.tar"],
        }
        files = {
            "manifest.json": json.dumps([image] * image_count).encode(),
            config_name: json.dumps({"architecture": "amd64", "os": "linux"}).encode(),
        }
        with tarfile.open(path, mode="w") as archive:
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
