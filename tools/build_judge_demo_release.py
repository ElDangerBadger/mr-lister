"""Build only the judge Lambda import closure using already pinned local wheels.

No downloads, AWS calls, secrets, or existing application artifacts are modified.
Use the checked Phase6 Lambda wheel inventory plus the four already checked AgentCore
JWT/crypto wheels. The resulting ZIP is Python 3.12/Linux ARM64, not a host-env copy.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import deque
from hashlib import sha256
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from tools.build_phase718_enabled_release import (
    _CAPABILITY_FREE_INITIALIZERS,
    _local_imports,
    _module_path,
    _parent_packages,
    _third_party_import_roots,
)

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = ("mr_lister.judge_session.entrypoint", "mr_lister.judge_cleanup.entrypoint")
EXTRA_WHEELS = frozenset({"pyjwt", "cryptography", "cffi", "pycparser"})


def source_closure(root: Path = ROOT) -> dict[str, Path]:
    queue, result = deque(ENTRYPOINTS), {}
    while queue:
        name = queue.popleft()
        if name in result:
            continue
        source = _module_path(root / "src", name)
        if source is None:
            raise ValueError("A judge source dependency is missing")
        result[name] = source
        queue.extend(_parent_packages(name))
        if name not in _CAPABILITY_FREE_INITIALIZERS:
            queue.extend(sorted(_local_imports(root / "src", name, source)))
    if _third_party_import_roots(result) != ("PIL", "boto3", "botocore", "jwt", "pydantic"):
        raise ValueError("Judge source dependency boundary changed")
    return dict(sorted(result.items()))


def wheel_inventory(root: Path = ROOT) -> list[dict]:
    config = root / "config/release/phase6"
    base = json.loads((config / "phase6-lambda-wheel-authority.json").read_text())
    extra = json.loads((config / "phase6-agentcore-wheel-authority.json").read_text())
    if base["target"] != extra["target"] or base["target"]["platform"] != "manylinux2014_aarch64":
        raise ValueError("Dependency target differs")
    wheels = list(base["wheels"])
    selected = [w for w in extra["wheels"] if w["name"].lower() in EXTRA_WHEELS]
    if {w["name"].lower() for w in selected} != EXTRA_WHEELS:
        raise ValueError("The checked JWT dependencies are incomplete")
    wheels.extend(selected)
    if len({w["name"].lower() for w in wheels}) != len(wheels):
        raise ValueError("Dependency inventories overlap")
    return sorted(wheels, key=lambda w: w["name"].lower())


def build(destination: Path, *, wheelhouses: list[Path], allow_dirty_draft: bool = False) -> dict:
    source_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True))
    if dirty and not allow_dirty_draft:
        raise ValueError("Commit the reviewed source before building a deployable artifact")
    if destination.exists():
        raise ValueError("Use a new destination directory")
    modules = source_closure()
    files = {}
    for name, source in modules.items():
        files[source.relative_to(ROOT / "src").as_posix()] = (
            b"" if name in _CAPABILITY_FREE_INITIALIZERS else source.read_bytes()
        )
    wheels = wheel_inventory()
    for wheel in wheels:
        paths = [folder / wheel["filename"] for folder in wheelhouses]
        candidates = [p for p in paths if p.is_file() and not p.is_symlink()]
        if not candidates or any(
            sha256(p.read_bytes()).hexdigest() != wheel["sha256"] for p in candidates
        ):
            raise ValueError("A checked dependency wheel is absent or changed")
        with ZipFile(candidates[0]) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                path = PurePosixPath(item.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Wheel has an unsupported installation path")
                if path.parts[0].endswith(".data"):
                    if len(path.parts) > 2 and path.parts[1] == "scripts":
                        # Lambda invokes our handlers; wheel command-line launchers are unused.
                        continue
                    raise ValueError("Wheel has an unsupported data installation path")
                if item.filename in files or path.parts[0] == "mr_lister":
                    raise ValueError("Wheel overlaps application source")
                if path.suffix in {".pyc", ".dylib", ".dll", ".pyd"}:
                    raise ValueError("Wheel contains a host-specific or compiled Python file")
                files[item.filename] = archive.read(item)
    destination.mkdir(parents=True, mode=0o700)
    archive_path = destination / "judge-demo.zip"
    with ZipFile(archive_path, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(files.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    manifest = {
        "format": "judge-demo-release-v1",
        "source_commit": source_commit,
        "deployable": not dirty,
        "entrypoints": list(ENTRYPOINTS),
        "target": "cp312-linux-arm64",
        "archive_sha256": sha256(archive_path.read_bytes()).hexdigest(),
        "archive_bytes": archive_path.stat().st_size,
        "wheels": wheels,
        "source_files": [
            {
                "path": path.relative_to(ROOT / "src").as_posix(),
                "sha256": sha256(files[path.relative_to(ROOT / "src").as_posix()]).hexdigest(),
            }
            for path in modules.values()
        ],
    }
    (destination / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, action="append", required=True)
    parser.add_argument("--allow-dirty-draft", action="store_true")
    args = parser.parse_args()
    manifest = build(
        args.output, wheelhouses=args.wheelhouse, allow_dirty_draft=args.allow_dirty_draft
    )
    print(
        json.dumps(
            {key: manifest[key] for key in ("deployable", "archive_sha256", "archive_bytes")}
        )
    )


if __name__ == "__main__":
    main()
