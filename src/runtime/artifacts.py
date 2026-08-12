"""Read-only inventory generation for docker-save OCI source archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence


class ArtifactInventoryError(ValueError):
    """Raised when an archive cannot provide an unambiguous golden identity."""


@dataclass(frozen=True)
class OciArchiveInventory:
    """Stable source identity recorded before converting an OCI archive to SIF."""

    schema_version: int
    source_filename: str
    source_size_bytes: int
    source_sha256: str
    config_digest: str
    repo_tags: tuple[str, ...]
    operating_system: str
    architecture: str
    layer_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Hash an archive without loading it into memory or changing it."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_docker_save_archive(path: str | Path) -> OciArchiveInventory:
    """Return the source identity required by the golden SIF build gate."""

    archive_path = Path(path).expanduser().resolve(strict=True)
    if not archive_path.is_file():
        raise ArtifactInventoryError(f"archive is not a regular file: {archive_path}")

    source_sha256 = sha256_file(archive_path)
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            manifest = _read_json_member(archive, "manifest.json")
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise ArtifactInventoryError(
                    "docker-save archive must contain exactly one image manifest"
                )

            image = manifest[0]
            if not isinstance(image, dict):
                raise ArtifactInventoryError("image manifest must be a JSON object")

            config_name = _required_string(image, "Config")
            config = _read_json_member(archive, config_name)
    except (tarfile.TarError, OSError) as exc:
        raise ArtifactInventoryError(f"invalid docker-save archive: {exc}") from exc

    if not isinstance(config, dict):
        raise ArtifactInventoryError("image config must be a JSON object")

    repo_tags = image.get("RepoTags") or []
    layers = image.get("Layers") or []
    if not isinstance(repo_tags, list) or not all(
        isinstance(tag, str) for tag in repo_tags
    ):
        raise ArtifactInventoryError("RepoTags must be a list of strings")
    if not isinstance(layers, list) or not all(
        isinstance(layer, str) for layer in layers
    ):
        raise ArtifactInventoryError("Layers must be a list of strings")

    return OciArchiveInventory(
        schema_version=1,
        source_filename=archive_path.name,
        source_size_bytes=archive_path.stat().st_size,
        source_sha256=source_sha256,
        config_digest=Path(config_name).stem,
        repo_tags=tuple(repo_tags),
        operating_system=_required_string(config, "os"),
        architecture=_required_string(config, "architecture"),
        layer_count=len(layers),
    )


def _read_json_member(archive: tarfile.TarFile, name: str) -> Any:
    member = archive.getmember(name)
    if not member.isfile():
        raise ArtifactInventoryError(f"archive member is not a file: {name}")
    source = archive.extractfile(member)
    if source is None:
        raise ArtifactInventoryError(f"archive member cannot be read: {name}")
    try:
        return json.load(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactInventoryError(
            f"archive member is not valid JSON: {name}"
        ) from exc


def _required_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ArtifactInventoryError(f"missing non-empty string field: {key}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a read-only source inventory for docker-save archives."
    )
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args(argv)
    inventories = [
        inspect_docker_save_archive(path).to_dict() for path in args.archives
    ]
    print(json.dumps(inventories, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
