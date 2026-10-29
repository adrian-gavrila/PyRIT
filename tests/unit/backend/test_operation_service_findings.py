# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import re

import pytest

from pyrit.backend.models.operations import FindingUpdateRequest
from pyrit.backend.services.operation_service import OperationService
from pyrit.memory import SQLiteMemory
from pyrit.models import Finding, FindingField, FindingFields, FindingValuesError, Operation
from pyrit.setup.finding_fields import set_active_finding_fields

pytestmark = pytest.mark.usefixtures("patch_central_database")

CONTOSO = FindingFields(
    severity=FindingField(
        key="severity", label="Severity", type="choice", options=[("sev1", "Sev 1"), ("sev2", "Sev 2")]
    ),
    fields=[FindingField(key="steps", label="Steps to reproduce", type="multiline", required=True)],
)
STORED_VALUES = {"steps": "1. Open", "description": "Old text"}


@pytest.fixture
async def retired_value_finding_async(sqlite_instance: SQLiteMemory, reset_finding_fields: None) -> Finding:
    """Seed a finding with a ``description`` value, then make CONTOSO, which has no such field, active."""
    operation = await sqlite_instance.add_operation_async(Operation(name="Contoso"))
    finding = await sqlite_instance.add_finding_async(
        finding=Finding(operation_id=operation.id, title="Leak", severity="sev1", field_values=STORED_VALUES)
    )
    set_active_finding_fields(finding_fields=CONTOSO, source="test")
    return finding


async def _update_async(
    *, finding: Finding, field_values: dict[str, str], remove_retired_keys: list[str] | None = None
) -> Finding:
    request = FindingUpdateRequest(
        title="Leak", severity="sev1", field_values=field_values, remove_retired_keys=remove_retired_keys or []
    )
    return await OperationService().update_finding_async(
        operation_id=finding.operation_id, finding_id=finding.id, request=request
    )


async def _stored_values_async(*, sqlite_instance: SQLiteMemory, finding: Finding) -> dict[str, str]:
    stored = await sqlite_instance.get_finding_async(operation_id=finding.operation_id, finding_id=finding.id)
    assert stored is not None
    return stored.field_values


async def test_update_keeps_an_omitted_retired_value(
    sqlite_instance: SQLiteMemory, retired_value_finding_async: Finding
) -> None:
    finding = retired_value_finding_async
    updated = await _update_async(finding=finding, field_values={"steps": "1. Open"})
    assert updated.field_values == STORED_VALUES
    assert await _stored_values_async(sqlite_instance=sqlite_instance, finding=finding) == STORED_VALUES


async def test_update_keeps_a_blank_retired_value(
    sqlite_instance: SQLiteMemory, retired_value_finding_async: Finding
) -> None:
    finding = retired_value_finding_async
    await _update_async(finding=finding, field_values={"steps": "1. Open", "description": " "})
    assert await _stored_values_async(sqlite_instance=sqlite_instance, finding=finding) == STORED_VALUES


async def test_update_removes_a_listed_retired_value(
    sqlite_instance: SQLiteMemory, retired_value_finding_async: Finding
) -> None:
    finding = retired_value_finding_async
    updated = await _update_async(
        finding=finding, field_values={"steps": "1. Open"}, remove_retired_keys=["description"]
    )
    assert updated.field_values == {"steps": "1. Open"}
    assert await _stored_values_async(sqlite_instance=sqlite_instance, finding=finding) == {"steps": "1. Open"}


@pytest.mark.parametrize("key", ["steps", "nonexistent"])
async def test_update_rejects_removing_a_value_that_is_not_retired(
    sqlite_instance: SQLiteMemory, retired_value_finding_async: Finding, key: str
) -> None:
    finding = retired_value_finding_async
    with pytest.raises(FindingValuesError, match=re.escape(f"{key!r} is not a retired value of this finding.")):
        await _update_async(finding=finding, field_values={"steps": "1. Open"}, remove_retired_keys=[key])
    assert await _stored_values_async(sqlite_instance=sqlite_instance, finding=finding) == STORED_VALUES


async def test_update_rejects_sending_and_removing_a_value(
    sqlite_instance: SQLiteMemory, retired_value_finding_async: Finding
) -> None:
    finding = retired_value_finding_async
    with pytest.raises(FindingValuesError, match=re.escape("'description' can't be both sent and removed.")):
        await _update_async(finding=finding, field_values=STORED_VALUES, remove_retired_keys=["description"])
    assert await _stored_values_async(sqlite_instance=sqlite_instance, finding=finding) == STORED_VALUES


async def test_update_rejects_changing_a_retired_value(
    sqlite_instance: SQLiteMemory, retired_value_finding_async: Finding
) -> None:
    finding = retired_value_finding_async
    message = "Finding field 'description' is no longer configured, so its saved value can't change."
    with pytest.raises(FindingValuesError, match=re.escape(message)):
        await _update_async(finding=finding, field_values={"steps": "1. Open", "description": "New text"})
    assert await _stored_values_async(sqlite_instance=sqlite_instance, finding=finding) == STORED_VALUES
