# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest

import pyrit.setup.initializers
from pyrit.models import DEFAULT_FINDING_FIELDS, FindingField, FindingFields
from pyrit.registry import InitializerRegistry
from pyrit.registry.discovery import discover_in_directory
from pyrit.setup.finding_fields import get_active_finding_fields, reset_active_finding_fields
from pyrit.setup.initializers import FindingFieldsInitializer
from pyrit.setup.pyrit_initializer import PyRITInitializer

EXAMPLE = FindingFields(
    severity=FindingField(key="severity", label="Severity", type="choice", options=[("high", "High")])
)

CONTOSO_SCRIPT = '''
from pyrit.models import FindingField, FindingFields
from pyrit.setup.initializers import FindingFieldsInitializer


class ContosoFindingFields(FindingFieldsInitializer):
    """Contoso finding fields."""

    def get_finding_fields(self) -> FindingFields:
        return FindingFields(
            severity=FindingField(key="severity", label="Severity", type="choice", options=[("sev1", "Sev 1")])
        )
'''


class ExampleFindingFields(FindingFieldsInitializer):
    """Example finding fields."""

    def get_finding_fields(self) -> FindingFields:
        return EXAMPLE


@pytest.fixture(autouse=True)
def reset_finding_fields() -> Iterator[None]:
    yield
    reset_active_finding_fields()


async def test_finding_fields_initializer_sets_the_active_fields() -> None:
    await ExampleFindingFields().initialize_async()
    assert get_active_finding_fields() is EXAMPLE


@pytest.mark.usefixtures("patch_central_database")
async def test_get_info_leaves_the_active_fields_alone() -> None:
    await ExampleFindingFields.get_info_async()
    info = await ExampleFindingFields.get_info_async()

    assert get_active_finding_fields() is DEFAULT_FINDING_FIELDS
    assert info["default_values"] == []
    assert info["global_variables"] == []


def test_finding_fields_initializer_is_skipped_by_builtin_discovery() -> None:
    directory = Path(pyrit.setup.initializers.__file__).parent
    names = [
        cls.__name__
        for _, _, cls in discover_in_directory(directory=directory, base_class=PyRITInitializer, recursive=True)
    ]
    assert "FindingFieldsInitializer" not in names


async def test_discovery_copy_shares_the_active_finding_fields() -> None:
    path = Path(pyrit.setup.initializers.__file__).parent / "finding_fields.py"
    spec = importlib.util.spec_from_file_location("discovered_module.finding_fields", path)
    copy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copy)

    class CopiedFindingFields(copy.FindingFieldsInitializer):
        def get_finding_fields(self) -> FindingFields:
            return EXAMPLE

    await CopiedFindingFields().initialize_async()
    assert get_active_finding_fields() is EXAMPLE


async def test_custom_script_subclass_sets_the_active_fields(tmp_path: Path) -> None:
    script = tmp_path / "contoso_fields.py"
    script.write_text(CONTOSO_SCRIPT)
    [initializer] = InitializerRegistry.get_registry_singleton().create_from_script_paths(
        script_paths=[script], strict=True
    )
    assert type(initializer).__name__ == "ContosoFindingFields"
    await initializer.initialize_async()
    assert get_active_finding_fields().severity_order == ["sev1"]
