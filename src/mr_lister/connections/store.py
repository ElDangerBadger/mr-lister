"""Owner-keyed exact-payload transactions; TTL is never an authorization decision."""

from __future__ import annotations

from botocore.exceptions import ClientError

from mr_lister.accounts.models import valid_owner

from .binding import StoreBindingAuthority
from .models import (
    VALIDATION_BUDGET_LIMIT,
    VALIDATION_BUDGET_WINDOW,
    Candidate,
    Connection,
    ConnectionConfig,
    ConnectionConflict,
    ConnectionError,
    ConnectionRateLimited,
    SelectionReceipt,
    Setup,
    StrictModel,
    ValidationBudget,
    canonical,
)


def setup_key(owner: str) -> str:
    if not valid_owner(owner):
        raise ConnectionError
    return f"OWNER#{owner}#SETUP"


def candidate_key(owner: str, candidate_id: str) -> str:
    return f"{setup_key(owner)}#CANDIDATE#{candidate_id}"


def budget_key(owner: str) -> str:
    return f"{setup_key(owner)}#VALIDATION_BUDGET"


def connection_key(binding: StoreBindingAuthority) -> str:
    return f"{setup_key(binding.owner_id)}#BINDING#{binding.shop_binding_id}"


def receipt_key(owner: str, operation: str) -> str:
    return f"{setup_key(owner)}#SELECT#{operation}"


def fence(connection: Connection) -> dict:
    binding = connection.binding
    return {
        "owner_id": {"S": binding.owner_id},
        "connection_id": {"S": binding.connection_id},
        "shop_binding_id": {"S": binding.shop_binding_id},
        "shop_id": {"N": str(binding.shop_id)},
        "authorization_epoch": {"N": str(binding.authorization_epoch)},
        "binding_fingerprint": {"S": binding.binding_fingerprint},
        "status": {"S": connection.state},
    }


def cleanup_fields(candidate: Candidate) -> dict:
    if candidate.state in ("consumed", "deleted"):
        return {}
    return {
        "cleanup_partition": {"S": "CANDIDATE"},
        "cleanup_due": {"N": str(candidate.expires_at + 901)},
    }


class DynamoConnectionDirectory:
    """Read capability shared by setup, pinned uploads, and exact provider claims."""

    def __init__(self, client: object, config: ConnectionConfig) -> None:
        self._client = client
        self.config = config

    def _owner(self, owner: str) -> None:
        if not valid_owner(owner) or owner in self.config.accounts.reserved_owner_ids:
            raise ConnectionError

    def _get(self, key: str, model: type[StrictModel]) -> StrictModel | None:
        try:
            result = self._client.get_item(
                TableName=self.config.table_name, Key={"PK": {"S": key}}, ConsistentRead=True
            )
            if not isinstance(result, dict):
                raise ValueError
            if "Item" not in result:
                return None
            item = result["Item"]
            fence_fields = (
                "owner_id",
                "connection_id",
                "shop_binding_id",
                "shop_id",
                "authorization_epoch",
                "binding_fingerprint",
                "status",
            )
            extra_candidate = (
                ({"cleanup_partition", "cleanup_due"} & set(item)) if model is Candidate else set()
            )
            if (
                not isinstance(item, dict)
                or set(item)
                != (
                    {"PK", "entity_type", "payload"}
                    | (set(fence_fields) if model is Connection else extra_candidate)
                )
                or item["PK"] != {"S": key}
                or item["entity_type"] != {"S": model.__name__}
                or not isinstance(item["payload"], dict)
                or set(item["payload"]) != {"S"}
            ):
                raise ValueError
            raw = item["payload"]["S"]
            from mr_lister.accounts.models import strict_json

            strict_json(raw, limit=65536)
            value = model.model_validate_json(raw)
            if model is Connection and any(
                item[name] != expected for name, expected in fence(value).items()
            ):
                raise ValueError
            if model is Candidate:
                actual = {name: item[name] for name in extra_candidate}
                if actual != cleanup_fields(value):
                    raise ValueError
            return value
        except Exception:
            pass
        raise ConnectionError

    def get_setup(self, owner: str) -> Setup:
        self._owner(owner)
        setup = self._get(setup_key(owner), Setup)
        if setup is None:
            return Setup(owner_id=owner)
        if setup.owner_id != owner:
            raise ConnectionError
        return setup

    def get_candidate(self, owner: str, candidate_id: str) -> Candidate | None:
        self._owner(owner)
        candidate = self._get(candidate_key(owner, candidate_id), Candidate)
        if candidate is not None and (
            candidate.owner_id != owner or candidate.candidate_id != candidate_id
        ):
            raise ConnectionError
        return candidate

    def get_receipt(self, owner: str, operation: str) -> SelectionReceipt | None:
        self._owner(owner)
        receipt = self._get(receipt_key(owner, operation), SelectionReceipt)
        if receipt is not None and receipt.owner_id != owner:
            raise ConnectionError
        return receipt

    def get_connection(self, binding: StoreBindingAuthority) -> Connection:
        binding = StoreBindingAuthority.model_validate(binding.model_dump())
        self._owner(binding.owner_id)
        connection = self._get(connection_key(binding), Connection)
        if connection is None or connection.binding != binding:
            raise ConnectionError
        expected_name = self.config.secret_prefix + binding.owner_id + "/" + connection.candidate_id
        if connection.secret.name != expected_name:
            raise ConnectionError
        return connection

    def get_active_binding(
        self, owner_id: str, shop_binding_id: str, expected_setup_version: int
    ) -> StoreBindingAuthority:
        setup = self.get_setup(owner_id)
        if (
            not self.config.workflow_enabled
            or not setup.seller_granted
            or setup.binding is None
            or setup.binding.shop_binding_id != shop_binding_id
            or type(expected_setup_version) is not int
            or setup.record_version != expected_setup_version
        ):
            raise ConnectionConflict
        self.require_current(setup.binding)
        return setup.binding.checked_for_owner(owner_id)

    def require_current(self, binding: StoreBindingAuthority) -> Connection:
        binding = StoreBindingAuthority.model_validate(binding.model_dump())
        connection = self.get_connection(binding)
        if not self.config.workflow_enabled or connection.state != "active":
            raise ConnectionConflict
        return connection

    def assert_current(self, binding: StoreBindingAuthority) -> None:
        self.require_current(binding)

    def current_epoch_condition(self, binding: StoreBindingAuthority) -> dict:
        connection = self.require_current(binding)
        values = fence(connection)
        names = {"#" + name: name for name in values}
        expressions = {":" + name: value for name, value in values.items()}
        names.update({"#payload": "payload", "#entity": "entity_type"})
        expressions.update(
            {":payload": {"S": canonical(connection)}, ":entity": {"S": "Connection"}}
        )
        return {
            "ConditionCheck": {
                "TableName": self.config.table_name,
                "Key": {"PK": {"S": connection_key(connection.binding)}},
                "ConditionExpression": " AND ".join("#" + name + " = :" + name for name in values)
                + " AND #payload = :payload AND #entity = :entity",
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": expressions,
            }
        }


class DynamoConnectionStore(DynamoConnectionDirectory):
    def _put(self, key: str, value: StrictModel, expected: StrictModel | None) -> dict:
        action = {
            "TableName": self.config.table_name,
            "Item": {
                "PK": {"S": key},
                "entity_type": {"S": type(value).__name__},
                "payload": {"S": canonical(value)},
            },
            "ConditionExpression": "attribute_not_exists(PK)",
        }
        if isinstance(value, Connection):
            action["Item"].update(fence(value))
        if isinstance(value, Candidate):
            action["Item"].update(cleanup_fields(value))
        if expected is not None:
            action.update(
                ConditionExpression="#payload = :payload AND #entity = :entity",
                ExpressionAttributeNames={"#payload": "payload", "#entity": "entity_type"},
                ExpressionAttributeValues={
                    ":payload": {"S": canonical(expected)},
                    ":entity": {"S": type(expected).__name__},
                },
            )
        if isinstance(expected, Connection):
            for name, value in fence(expected).items():
                action["ConditionExpression"] += " AND #" + name + " = :" + name
                action["ExpressionAttributeNames"]["#" + name] = name
                action["ExpressionAttributeValues"][":" + name] = value
        return {"Put": action}

    def _transact(self, actions: list[dict]) -> None:
        conflict = False
        try:
            self._client.transact_write_items(TransactItems=actions)
            return
        except ClientError as error:
            conflict = error.response.get("Error", {}).get("Code") in (
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            )
        except Exception:
            pass
        if conflict:
            raise ConnectionConflict
        raise ConnectionError

    def reserve_candidate(self, candidate: Candidate) -> Candidate:
        self._owner(candidate.owner_id)
        candidate = Candidate.model_validate(candidate.model_dump())
        if candidate.state != "pending":
            raise ConnectionConflict
        key = candidate_key(candidate.owner_id, candidate.candidate_id)
        existing = self.get_candidate(candidate.owner_id, candidate.candidate_id)
        if existing is not None:
            if existing.request_digest != candidate.request_digest:
                raise ConnectionConflict
            return existing
        # A reservation fences the unbound setup and charges one sliding-window attempt
        # in the same transaction that creates the candidate, before any secret is written.
        previous_setup = self._get(setup_key(candidate.owner_id), Setup)
        if previous_setup is not None and (
            previous_setup.owner_id != candidate.owner_id or previous_setup.binding is not None
        ):
            raise ConnectionConflict
        previous_budget = self._get(budget_key(candidate.owner_id), ValidationBudget)
        recent = ()
        if previous_budget is not None:
            if previous_budget.owner_id != candidate.owner_id or any(
                timestamp > candidate.created_at for timestamp in previous_budget.attempted_at
            ):
                raise ConnectionError
            recent = tuple(
                timestamp
                for timestamp in previous_budget.attempted_at
                if timestamp > candidate.created_at - VALIDATION_BUDGET_WINDOW
            )
        if len(recent) >= VALIDATION_BUDGET_LIMIT:
            raise ConnectionRateLimited
        budget = ValidationBudget(
            owner_id=candidate.owner_id, attempted_at=(*recent, candidate.created_at)
        )
        setup_check = {
            "TableName": self.config.table_name,
            "Key": {"PK": {"S": setup_key(candidate.owner_id)}},
            "ConditionExpression": "attribute_not_exists(PK)",
        }
        if previous_setup is not None:
            setup_check.update(
                ConditionExpression="#payload = :payload AND #entity = :entity",
                ExpressionAttributeNames={"#payload": "payload", "#entity": "entity_type"},
                ExpressionAttributeValues={
                    ":payload": {"S": canonical(previous_setup)},
                    ":entity": {"S": "Setup"},
                },
            )
        conflict = False
        try:
            self._transact(
                [
                    self._put(key, candidate, None),
                    self._put(budget_key(candidate.owner_id), budget, previous_budget),
                    {"ConditionCheck": setup_check},
                ]
            )
            return candidate
        except ConnectionError as error:
            conflict = isinstance(error, ConnectionConflict)
        existing = self.get_candidate(candidate.owner_id, candidate.candidate_id)
        if existing is not None and existing.request_digest == candidate.request_digest:
            return existing
        if existing is not None:
            raise ConnectionConflict
        if conflict:
            raise ConnectionConflict
        raise ConnectionError

    def publish_candidate(
        self, pending: Candidate, validated: Candidate, previous: Setup
    ) -> Candidate:
        self._owner(pending.owner_id)
        actions = [
            self._put(candidate_key(pending.owner_id, pending.candidate_id), validated, pending)
        ]
        if previous.binding is None:
            setup = Setup(
                owner_id=pending.owner_id,
                record_version=validated.record_version,
                candidate_id=validated.candidate_id,
            )
            old = (
                None if previous.record_version == 1 and previous.candidate_id is None else previous
            )
            actions.append(self._put(setup_key(pending.owner_id), setup, old))
        try:
            self._transact(actions)
            return validated
        except ConnectionError:
            pass
        current = self.get_candidate(pending.owner_id, pending.candidate_id)
        if current == validated:
            return current
        raise ConnectionConflict

    def select(
        self,
        candidate: Candidate,
        previous: Setup,
        connection: Connection,
        operation: str,
        request_digest: str,
    ) -> Setup:
        self._owner(candidate.owner_id)
        if previous.binding is not None:
            raise ConnectionConflict
        consumed = Candidate.model_validate({**candidate.model_dump(), "state": "consumed"})
        setup = Setup(
            owner_id=candidate.owner_id,
            record_version=previous.record_version + 1,
            binding=connection.binding,
        )
        receipt = SelectionReceipt(
            owner_id=candidate.owner_id, request_digest=request_digest, binding=connection.binding
        )
        self._transact(
            [
                self._put(
                    candidate_key(candidate.owner_id, candidate.candidate_id), consumed, candidate
                ),
                self._put(connection_key(connection.binding), connection, None),
                self._put(setup_key(candidate.owner_id), setup, previous),
                self._put(receipt_key(candidate.owner_id, operation), receipt, None),
            ]
        )
        return setup

    def mark_seller_granted(self, setup: Setup) -> Setup:
        connection = self.get_connection(setup.binding)
        active = Connection.model_validate({**connection.model_dump(), "state": "active"})
        updated = Setup.model_validate({**setup.model_dump(), "seller_granted": True})
        try:
            self._transact(
                [
                    self._put(setup_key(setup.owner_id), updated, setup),
                    self._put(connection_key(connection.binding), active, connection),
                ]
            )
            return updated
        except ConnectionError:
            pass
        current = self.get_setup(setup.owner_id)
        if current == updated and self.get_connection(active.binding) == active:
            return current
        raise ConnectionConflict

    def replace_candidate(self, previous: Candidate, current: Candidate) -> Candidate:
        self._owner(previous.owner_id)
        if current.owner_id != previous.owner_id or current.candidate_id != previous.candidate_id:
            raise ConnectionConflict
        try:
            self._transact(
                [
                    self._put(
                        candidate_key(previous.owner_id, previous.candidate_id), current, previous
                    )
                ]
            )
            return current
        except ConnectionError:
            pass
        found = self.get_candidate(previous.owner_id, previous.candidate_id)
        if found == current:
            return found
        raise ConnectionConflict

    def claim_cleanup(self, candidate: Candidate) -> Candidate:
        self._owner(candidate.owner_id)
        connection_pk = f"{setup_key(candidate.owner_id)}#BINDING#{candidate.shop_binding_id}"
        # Any original binding row makes disposal unsafe, including malformed or pending rows.
        existing = self._get(connection_pk, Connection)
        if existing is not None:
            raise ConnectionConflict
        deleting = Candidate.model_validate({**candidate.model_dump(), "state": "deleting"})
        self._transact(
            [
                self._put(
                    candidate_key(candidate.owner_id, candidate.candidate_id), deleting, candidate
                ),
                {
                    "ConditionCheck": {
                        "TableName": self.config.table_name,
                        "Key": {"PK": {"S": connection_pk}},
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
            ]
        )
        return deleting

    def due_candidates(self, *, now: int, index_name: str) -> tuple[tuple[str, str], ...]:
        import re

        if index_name != "CandidateCleanupDue" or type(now) is not int or now <= 0:
            raise ConnectionError
        try:
            result = self._client.query(
                TableName=self.config.table_name,
                IndexName=index_name,
                KeyConditionExpression="cleanup_partition = :partition AND cleanup_due <= :due",
                ExpressionAttributeValues={
                    ":partition": {"S": "CANDIDATE"},
                    ":due": {"N": str(now)},
                },
                ProjectionExpression="PK",
                Limit=10,
                ScanIndexForward=True,
            )
            items = result.get("Items")
            if not isinstance(items, list) or len(items) > 10:
                raise ValueError
            targets = []
            for item in items:
                key = item["PK"]["S"]
                match = re.fullmatch(
                    r"OWNER#([a-f0-9]{64})#SETUP#CANDIDATE#(candidate_[a-f0-9]{32})", key
                )
                if match is None:
                    raise ValueError
                self._owner(match[1])
                targets.append((match[1], match[2]))
            if len(set(targets)) != len(targets):
                raise ValueError
            return tuple(targets)
        except Exception:
            pass
        raise ConnectionError
