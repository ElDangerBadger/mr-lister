"""Targeted expired-candidate cleanup; no scan, secret discovery, or active-secret deletion.

The dispatcher must retain explicit owner/candidate references. The 900-second grace exceeds
an in-flight validation Lambda's maximum remaining lifetime after candidate expiry. Do not
configure DynamoDB TTL on candidate metadata: it is the authoritative cleanup work record.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

from .models import Candidate, ConnectionConfig, ConnectionConflict, ConnectionError

CLEANUP_GRACE_SECONDS = 900


class CandidateSecretDisposer:
    def __init__(self, client: object, config: ConnectionConfig) -> None:
        self._client = client
        self._config = config

    def schedule_delete(self, candidate: Candidate) -> None:
        try:
            candidate = Candidate.model_validate(candidate.model_dump())
            if (
                candidate.state != "deleting"
                or candidate.owner_id in self._config.accounts.reserved_owner_ids
            ):
                raise ValueError
            name = self._config.secret_prefix + candidate.owner_id + "/" + candidate.candidate_id
            result = self._client.delete_secret(SecretId=name, RecoveryWindowInDays=7)
            if result.get("Name") != name:
                raise ValueError
            return
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return
        except Exception:
            pass
        raise ConnectionError


class CandidateCleanupService:
    def __init__(self, *, store: object, disposer: CandidateSecretDisposer, clock) -> None:
        self._store = store
        self._disposer = disposer
        self._clock = clock

    def cleanup(self, *, owner_id: str, candidate_id: str) -> str:
        candidate = self._store.get_candidate(owner_id, candidate_id)
        if candidate is None:
            return "absent"
        if candidate.state in ("consumed", "deleted"):
            return candidate.state
        if candidate.expires_at + CLEANUP_GRACE_SECONDS >= self._clock():
            raise ConnectionConflict
        candidate = self._store.claim_cleanup(candidate)
        # The consumed/selected transaction and this claim compare the same candidate row.
        # Once deleting is committed, selection and validation publication cannot win.
        self._disposer.schedule_delete(candidate)
        deleted = Candidate.model_validate({**candidate.model_dump(), "state": "deleted"})
        self._store.replace_candidate(candidate, deleted)
        return "deleted"

    def cleanup_batch(self, targets: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
        if not isinstance(targets, tuple) or not 1 <= len(targets) <= 25:
            raise ConnectionConflict
        return tuple(
            self.cleanup(owner_id=owner, candidate_id=candidate) for owner, candidate in targets
        )

    def run_due(self, *, index_name: str) -> dict:
        targets = self._store.due_candidates(now=self._clock(), index_name=index_name)
        completed = 0
        failed = 0
        for owner, candidate in targets:
            try:
                self.cleanup(owner_id=owner, candidate_id=candidate)
                completed += 1
            except Exception:
                failed += 1
        return {"processed": len(targets), "completed": completed, "failed": failed}
