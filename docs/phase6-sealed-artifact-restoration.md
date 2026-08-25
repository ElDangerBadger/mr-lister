# Phase 6 sealed-artifact restoration

The restoration command recovers a preserved, already-sealed Phase 6 release into the canonical
ignored deployment paths. It does not build dependencies, rebuild source bundles, change the
release fingerprint, or reseal anything. It makes no AWS call.

The preservation directory name is part of the authority boundary. It must be exactly
`phase6-artifacts-<first-eight-release-fingerprint>`, and it must contain only:

- `phase6-agentcore.zip`
- `phase6-lambda.zip`
- `deployment-descriptor.json`

From the repository root, restore with:

```shell
.venv/bin/python -m tools.restore_phase6_deployment_artifacts \
  .mr_lister_private/phase6-artifacts-<first-eight-release-fingerprint>
```

The destinations are fixed by default to `.mr_lister_private/phase6-deployment` and
`.mr_lister_private/phase6-artifacts`. Both must be absent. The command treats an existing empty
directory as an overwrite conflict and never deletes or replaces it; inspect and relocate any
existing material yourself before restoration.

Before publishing either destination, the command requires the descriptor's canonical JSON,
schema, release-prefix binding, archive names, sizes, and SHA-256 values to match. It rejects
extra input files, symlinks in any source or destination path component (including ancestors above
the immediate parent), path traversal, absolute or ambiguous ZIP paths, duplicate or
case-colliding members, file/directory collisions, non-regular members, and non-deterministic ZIP
metadata. Extraction occurs in private staging without `ZipFile.extract`.

The staged trees and exact three artifact files must pass
`verify_phase6_deployment_artifacts(..., verify_current_source=True)`. The command then reserves
new canonical destinations, moves the already-verified bytes into them, and runs the same
current-source verification against the final paths. A final failure removes only the two new
directories created by that invocation. The preserved source directory remains unchanged.
