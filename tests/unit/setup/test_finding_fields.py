# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from collections.abc import Iterator

import pytest

from pyrit.models import DEFAULT_FINDING_FIELDS, FindingField, FindingFields
from pyrit.setup.finding_fields import (
    get_active_finding_fields,
    reset_active_finding_fields,
    set_active_finding_fields,
)

CUSTOM = FindingFields(
    severity=FindingField(key="severity", label="Severity", type="choice", options=[("high", "High")])
)


@pytest.fixture(autouse=True)
def reset_finding_fields() -> Iterator[None]:
    yield
    reset_active_finding_fields()


def test_active_finding_fields_default_to_3066() -> None:
    assert get_active_finding_fields() is DEFAULT_FINDING_FIELDS


def test_set_active_finding_fields_replaces_the_default() -> None:
    set_active_finding_fields(finding_fields=CUSTOM, source="First")
    assert get_active_finding_fields() is CUSTOM


def test_set_active_finding_fields_twice_names_both_sources() -> None:
    set_active_finding_fields(finding_fields=CUSTOM, source="First")
    with pytest.raises(ValueError, match="Second cannot set the finding fields: First already set them"):
        set_active_finding_fields(finding_fields=DEFAULT_FINDING_FIELDS, source="Second")
    assert get_active_finding_fields() is CUSTOM


def test_reset_restores_the_default() -> None:
    set_active_finding_fields(finding_fields=CUSTOM, source="First")
    reset_active_finding_fields()
    assert get_active_finding_fields() is DEFAULT_FINDING_FIELDS
