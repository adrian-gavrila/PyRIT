# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, create_autospec, patch
from uuid import uuid4

import pytest

from pyrit.models import Message, MessagePiece
from pyrit.prompt_normalizer import PromptNormalizer

if TYPE_CHECKING:
    from pyrit.memory import MemoryInterface


def _assistant_reply(text: str) -> Any:
    """Build an SDK assistant-message event carrying ``text``."""
    from copilot.generated.session_events import AssistantMessageData, SessionEvent, SessionEventType

    return SessionEvent(
        id=uuid4(),
        timestamp=datetime.now(UTC),
        type=SessionEventType.ASSISTANT_MESSAGE,
        data=AssistantMessageData(content=text, message_id="sdk-reply"),
    )


def _mock_copilot_client(sdk: Any, *, reply: Any = None, error: Exception | None = None) -> Any:
    """Build an autospec Copilot client whose session returns ``reply`` or raises ``error``.

    The target uses the client as an async context manager, so ``__aenter__`` must yield the
    same mock the assertions inspect. ``session_id`` is set explicitly because the real
    attribute is assigned in ``__init__`` and therefore absent from the class spec.
    """
    session = create_autospec(sdk.CopilotSession, instance=True)
    session.session_id = "sdk-session-id"
    session.send_and_wait = AsyncMock(return_value=reply, side_effect=error)
    client = create_autospec(sdk.CopilotClient, instance=True)
    client.__aenter__.return_value = client
    client.create_session.return_value = session
    return client


@pytest.mark.usefixtures("patch_central_database")
async def test_normalizer_returns_and_stores_copilot_reply_async(sqlite_instance: MemoryInterface) -> None:
    from pyrit.prompt_target import GitHubCopilotTarget

    sdk = pytest.importorskip("copilot")

    client = _mock_copilot_client(sdk, reply=_assistant_reply("HELLO"))
    conversation_id = str(uuid4())

    with patch.object(sdk, "CopilotClient", return_value=client):
        target = GitHubCopilotTarget(model_name="gpt-5-mini")
        response = await PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
            conversation_id=conversation_id,
            target=target,
        )

    assert isinstance(response, Message)
    piece = response.get_piece()
    assert (piece.role, piece.converted_value, piece.conversation_id, piece.response_error) == (
        "assistant",
        "HELLO",
        conversation_id,
        "none",
    )
    stored_messages = sqlite_instance.get_conversation_messages(conversation_id=conversation_id)
    assert [(message.get_piece().role, message.get_piece().converted_value) for message in stored_messages] == [
        ("user", "Reply exactly HELLO."),
        ("assistant", "HELLO"),
    ]


@pytest.mark.usefixtures("patch_central_database")
async def test_create_session_restricts_copilot_runtime_async() -> None:
    from pyrit.prompt_target import GitHubCopilotTarget

    sdk = pytest.importorskip("copilot")
    from copilot.generated.rpc import RemoteSessionMode

    restricted_configuration = {
        "remote_session": RemoteSessionMode.OFF,
        "available_tools": [],
        "skip_custom_instructions": True,
        "instruction_directories": [],
        "enable_host_git_operations": False,
        "enable_config_discovery": False,
        "organization_custom_instructions": "",
        "enable_on_demand_instruction_discovery": False,
    }
    client = _mock_copilot_client(sdk, reply=_assistant_reply("HELLO"))

    with patch.object(sdk, "CopilotClient", return_value=client):
        await PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
            conversation_id=str(uuid4()),
            target=GitHubCopilotTarget(model_name="gpt-5-mini"),
        )

    sent_configuration = client.create_session.call_args.kwargs
    assert {name: sent_configuration.get(name) for name in restricted_configuration} == restricted_configuration


@pytest.mark.usefixtures("patch_central_database")
async def test_owned_session_is_deleted_after_successful_send_async() -> None:
    from pyrit.prompt_target import GitHubCopilotTarget

    sdk = pytest.importorskip("copilot")

    client = _mock_copilot_client(sdk, reply=_assistant_reply("HELLO"))

    with patch.object(sdk, "CopilotClient", return_value=client):
        await PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
            conversation_id=str(uuid4()),
            target=GitHubCopilotTarget(model_name="gpt-5-mini"),
        )

    client.delete_session.assert_awaited_once_with("sdk-session-id")
    client.__aexit__.assert_awaited_once()


@pytest.mark.usefixtures("patch_central_database")
async def test_owned_session_is_deleted_when_send_fails_async() -> None:
    from pyrit.prompt_target import GitHubCopilotTarget

    sdk = pytest.importorskip("copilot")

    client = _mock_copilot_client(sdk, error=RuntimeError("copilot runtime exploded"))
    target = GitHubCopilotTarget(model_name="gpt-5-mini")
    request = MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message()

    with patch.object(sdk, "CopilotClient", return_value=client):
        with pytest.raises(RuntimeError, match="copilot runtime exploded"):
            await target._send_prompt_to_target_async(normalized_conversation=[request])

    client.delete_session.assert_awaited_once_with("sdk-session-id")
    client.__aexit__.assert_awaited_once()


@pytest.mark.usefixtures("patch_central_database")
async def test_retained_session_is_kept_and_reported_async(caplog: pytest.LogCaptureFixture) -> None:
    from pyrit.prompt_target import GitHubCopilotTarget

    sdk = pytest.importorskip("copilot")

    client = _mock_copilot_client(sdk, reply=_assistant_reply("HELLO"))
    target = GitHubCopilotTarget(model_name="gpt-5-mini", retain_session=True)

    with patch.object(sdk, "CopilotClient", return_value=client), caplog.at_level(logging.INFO):
        await PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
            conversation_id=str(uuid4()),
            target=target,
        )

    client.delete_session.assert_not_awaited()
    client.__aexit__.assert_awaited_once()
    assert "sdk-session-id" in caplog.text
