"""Prepare one canonical, private Phase 6 seller-web release manifest.

The tool reads only an explicit, already-built ``web/dist`` directory.  It does not build the
application, contact AWS, or upload anything.  The accepted bundle is deliberately closed to the
four files proven by the Phase 6.6 browser gate; runtime configuration remains a separate
post-deployment artifact.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = Path(".mr_lister_private")
MANIFEST_FORMAT = "mr-lister-phase6-web-release-v1"
BROWSER_GATE_BUNDLE_SHA256 = "b43eb427c0e26b5151c741b978db2f881b3780bf315f1a3f2f3c87c790addb7a"

EXPECTED_WEB_RELEASE_FILES: Mapping[str, tuple[str, str]] = {
    "assets/index-BiMmzyh5.css": (
        "text/css; charset=utf-8",
        "public, max-age=31536000, immutable",
    ),
    "assets/index-LH8ry4bk.js": (
        "text/javascript; charset=utf-8",
        "public, max-age=31536000, immutable",
    ),
    "favicon.svg": (
        "image/svg+xml",
        "private, no-store, max-age=0",
    ),
    "index.html": (
        "text/html; charset=utf-8",
        "private, no-store, max-age=0",
    ),
}

_EXPECTED_DIRECTORIES = frozenset({"assets"})
_GENERIC_ERROR = "Phase 6 web release input is invalid"


class Phase6WebReleaseError(RuntimeError):
    """Value-free failure for unsafe, incomplete, or drifting release input."""


def render_phase6_web_release_manifest(
    dist_directory: Path,
    *,
    repository_root: Path = ROOT,
) -> bytes:
    """Return canonical manifest bytes for one exact seller-web distribution tree."""

    try:
        repository = repository_root.resolve(strict=True)
        if repository_root.is_symlink() or not repository.is_dir():
            raise ValueError
        dist = dist_directory.resolve(strict=True)
        if (
            dist_directory.is_symlink()
            or not dist.is_dir()
            or not dist.is_relative_to(repository)
            or _path_has_symlink_component(repository, dist)
        ):
            raise ValueError

        discovered_files: dict[str, Path] = {}
        discovered_directories: set[str] = set()
        for path in dist.rglob("*"):
            if path.is_symlink():
                raise ValueError
            relative = path.relative_to(dist).as_posix()
            if path.is_dir():
                discovered_directories.add(relative)
                continue
            if not path.is_file():
                raise ValueError
            if path.suffix.casefold() == ".map" or path.name.casefold() in {
                "runtime-config.json",
                "runtime-config.example.json",
            }:
                raise ValueError
            discovered_files[relative] = path

        if (
            set(discovered_files) != set(EXPECTED_WEB_RELEASE_FILES)
            or discovered_directories != _EXPECTED_DIRECTORIES
        ):
            raise ValueError

        files: list[dict[str, object]] = []
        browser_digest = sha256()
        for relative, (content_type, cache_control) in sorted(EXPECTED_WEB_RELEASE_FILES.items()):
            contents = discovered_files[relative].read_bytes()
            if not contents:
                raise ValueError
            relative_bytes = relative.encode("utf-8")
            browser_digest.update(len(relative_bytes).to_bytes(4, "big"))
            browser_digest.update(relative_bytes)
            browser_digest.update(len(contents).to_bytes(8, "big"))
            browser_digest.update(sha256(contents).digest())
            files.append(
                {
                    "cache_control": cache_control,
                    "content_type": content_type,
                    "path": relative,
                    "sha256": sha256(contents).hexdigest(),
                    "size_bytes": len(contents),
                }
            )
        browser_bundle_sha256 = browser_digest.hexdigest()
        if browser_bundle_sha256 != BROWSER_GATE_BUNDLE_SHA256:
            raise ValueError

        authority: dict[str, object] = {
            "algorithm": "sha256",
            "browser_gate_bundle_sha256": browser_bundle_sha256,
            "file_count": len(files),
            "files": files,
            "format": MANIFEST_FORMAT,
        }
        manifest = {
            **authority,
            "aggregate_sha256": sha256(_canonical_json(authority)).hexdigest(),
        }
        return _canonical_json(manifest)
    except Phase6WebReleaseError:
        raise
    except Exception:
        raise Phase6WebReleaseError(_GENERIC_ERROR) from None


def write_phase6_web_release_manifest(
    dist_directory: Path,
    destination: Path,
    *,
    repository_root: Path = ROOT,
) -> Path:
    """Create one private manifest and refuse an existing or non-private destination."""

    try:
        repository = repository_root.resolve(strict=True)
        private_root = repository / PRIVATE_ROOT
        _prepare_private_directory(repository, private_root)

        target = destination if destination.is_absolute() else repository / destination
        target = target.resolve(strict=False)
        if (
            target == private_root
            or not target.is_relative_to(private_root)
            or target.exists()
            or target.is_symlink()
        ):
            raise ValueError
        _prepare_private_directory(private_root, target.parent)

        raw = render_phase6_web_release_manifest(
            dist_directory,
            repository_root=repository,
        )
        with target.open("xb") as stream:
            stream.write(raw)
        target.chmod(0o600)
        return target
    except Phase6WebReleaseError:
        raise
    except Exception:
        raise Phase6WebReleaseError(_GENERIC_ERROR) from None


def _prepare_private_directory(root: Path, directory: Path) -> None:
    if not directory.is_relative_to(root):
        raise ValueError
    relative = directory.relative_to(root)
    current = root
    for component in relative.parts:
        current = current / component
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise ValueError
        else:
            current.mkdir(mode=0o700)


def _path_has_symlink_component(root: Path, path: Path) -> bool:
    if not path.is_relative_to(root):
        return True
    current = root
    for component in path.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        print(write_phase6_web_release_manifest(arguments.dist, arguments.output))
    except Phase6WebReleaseError as error:
        parser.exit(2, f"{error}\n")


__all__ = [
    "BROWSER_GATE_BUNDLE_SHA256",
    "EXPECTED_WEB_RELEASE_FILES",
    "MANIFEST_FORMAT",
    "Phase6WebReleaseError",
    "render_phase6_web_release_manifest",
    "write_phase6_web_release_manifest",
]


if __name__ == "__main__":
    main()
