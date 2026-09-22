# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, NonCallableMagicMock, create_autospec, patch
from uuid import UUID, uuid4

import pytest

from pyrit.models import Message, MessagePiece
from pyrit.prompt_normalizer import PromptNormalizer
from pyrit.prompt_target import GitHubCopilotTarget

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from copilot.generated.session_events import SessionEvent

    from pyrit.memory import MemoryInterface

TARGET_LOGGER = "pyrit.prompt_target.github_copilot_target"


@pytest.fixture
def sdk() -> Any:
    return pytest.importorskip("copilot")


@pytest.fixture
def client(sdk: Any) -> Iterator[NonCallableMagicMock]:
    from copilot.client import GetStatusResponse

    session = create_autospec(sdk.CopilotSession, instance=True)
    session.session_id = "sdk-session-id"
    session.send_and_wait.return_value = _assistant_reply("HELLO")
    client = create_autospec(sdk.CopilotClient, instance=True)
    assert isinstance(client, NonCallableMagicMock)
    client.create_session.return_value = session
    client.get_status.return_value = GetStatusResponse(version="6.5.4", protocol_version=3)
    with patch.object(sdk, "CopilotClient", return_value=client):
        yield client


def _assistant_reply(text: str) -> SessionEvent:
    from copilot.generated.session_events import AssistantMessageData, SessionEvent, SessionEventType

    return SessionEvent(
        id=uuid4(),
        timestamp=datetime.now(UTC),
        type=SessionEventType.ASSISTANT_MESSAGE,
        data=AssistantMessageData(content=text, message_id="sdk-reply"),
    )


def _mock_session_storage(*, client: NonCallableMagicMock, sessions: set[str]) -> None:
    from copilot import SessionMetadata

    async def get_session_metadata_async(session_id: str) -> SessionMetadata | None:
        if session_id not in sessions:
            return None
        return SessionMetadata(
            session_id=session_id,
            start_time=datetime(2026, 1, 1, tzinfo=UTC),
            modified_time=datetime(2026, 1, 1, tzinfo=UTC),
            is_remote=False,
        )

    client.get_session_metadata.side_effect = get_session_metadata_async
    client.delete_session.side_effect = sessions.remove


@pytest.mark.usefixtures("patch_central_database")
@pytest.mark.parametrize("retain_session", [False, True], ids=["delete", "retain"])
async def test_normalizer_round_trip_and_retention_async(
    *,
    sdk: Any,
    client: NonCallableMagicMock,
    sqlite_instance: MemoryInterface,
    caplog: pytest.LogCaptureFixture,
    retain_session: bool,
) -> None:
    conversation_id = str(uuid4())
    request = MessagePiece(
        role="user", original_value="Original text before conversion.", converted_value="Reply exactly HELLO."
    ).to_message()
    with patch.object(sdk, "__version__", "9.8.7"), caplog.at_level(logging.INFO, logger=TARGET_LOGGER):
        response = await PromptNormalizer().send_prompt_async(
            message=request,
            conversation_id=conversation_id,
            target=GitHubCopilotTarget(model_name="gpt-5-mini", retain_session=retain_session),
        )

    assert isinstance(response, Message)
    piece = response.get_piece()
    assert (piece.role, piece.converted_value, piece.conversation_id, piece.response_error) == (
        "assistant",
        "HELLO",
        conversation_id,
        "none",
    )
    stored = sqlite_instance.get_conversation_messages(conversation_id=conversation_id)
    assert [
        (m.get_piece().role, m.get_piece().original_value, m.get_piece().converted_value, m.get_piece().response_error)
        for m in stored
    ] == [
        ("user", "Original text before conversion.", "Reply exactly HELLO.", "none"),
        ("assistant", "HELLO", "HELLO", "none"),
    ]
    session = client.create_session.return_value
    session.send_and_wait.assert_awaited_once_with("Reply exactly HELLO.", timeout=60.0)
    session.on.return_value.assert_called_once_with()
    client.start.assert_awaited_once()
    client.get_status.assert_awaited_once()
    client.create_session.assert_awaited_once()
    client.get_session_metadata.assert_not_awaited()
    client.stop.assert_awaited_once()
    sdk.CopilotClient.assert_called_once_with(github_token=None, working_directory=None)
    requested_id = client.create_session.await_args.kwargs["session_id"]
    assert str(UUID(requested_id)) == requested_id
    assert requested_id != "sdk-session-id"
    records = [r.getMessage() for r in caplog.records if r.name == TARGET_LOGGER and r.levelno == logging.INFO]
    assert len(records) == (2 if retain_session else 1)
    assert records[0].startswith("Attempting Copilot session creation:")
    for field in (
        f"pyrit_conversation_id={conversation_id}",
        f"requested_sdk_session_id={requested_id}",
        "sdk_version=9.8.7",
        "runtime_version=6.5.4",
        "runtime_protocol_version=3",
        f"retain_session={retain_session}",
        "remote_mode=OFF",
    ):
        assert field in records[0]
    if retain_session:
        client.delete_session.assert_not_awaited()
        assert records[1] == "Retaining Copilot session sdk-session-id as requested; delete it manually."
    else:
        client.delete_session.assert_awaited_once_with("sdk-session-id")


@pytest.mark.usefixtures("patch_central_database")
async def test_create_session_restricts_copilot_runtime_async(client: NonCallableMagicMock) -> None:
    from copilot.generated.rpc import RemoteSessionMode

    await PromptNormalizer().send_prompt_async(
        message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
        target=GitHubCopilotTarget(model_name="gpt-5-mini"),
    )
    configuration = dict(client.create_session.await_args.kwargs)
    assert str(UUID(configuration.pop("session_id")))
    assert configuration == {
        "model": "gpt-5-mini",
        "system_message": {
            "mode": "customize",
            "sections": {
                "environment_context": {"action": "remove"},
                "custom_instructions": {"action": "remove"},
            },
        },
        "remote_session": RemoteSessionMode.OFF,
        "available_tools": [],
        "skip_custom_instructions": True,
        "instruction_directories": [],
        "enable_host_git_operations": False,
        "enable_config_discovery": False,
        "organization_custom_instructions": "",
        "enable_on_demand_instruction_discovery": False,
        "infinite_sessions": {"enabled": False},
        "memory": {"enabled": False},
        "enable_session_store": False,
        "enable_file_hooks": False,
    }


@pytest.mark.usefixtures("patch_central_database", "sdk")
def test_target_advertises_single_turn_text_only_capabilities() -> None:
    capabilities = GitHubCopilotTarget(model_name="gpt-4o").capabilities
    assert capabilities.supports_multi_turn is False
    assert capabilities.supports_system_prompt is False
    assert capabilities.supports_multi_message_pieces is False
    assert capabilities.input_modalities == frozenset({frozenset({"text"})})
    assert capabilities.output_modalities == frozenset({frozenset({"text"})})


@pytest.mark.usefixtures("patch_central_database")
@pytest.mark.parametrize("failure_stage", ["start", "status", "send", "stop"])
async def test_normalizer_surfaces_lifecycle_failures_async(
    *,
    client: NonCallableMagicMock,
    sqlite_instance: MemoryInterface,
    failure_stage: str,
) -> None:
    from copilot.client import StopError

    session = client.create_session.return_value
    error = (
        ExceptionGroup("SDK shutdown failed", [StopError(message="Synthetic shutdown failure")])
        if failure_stage == "stop"
        else RuntimeError(f"Synthetic {failure_stage} failure")
    )
    operation = {
        "start": client.start,
        "status": client.get_status,
        "send": session.send_and_wait,
        "stop": client.stop,
    }[failure_stage]
    operation.side_effect = error
    conversation_id = str(uuid4())
    with pytest.raises(Exception, match="Error sending prompt with conversation ID:") as exc_info:
        await PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
            conversation_id=conversation_id,
            target=GitHubCopilotTarget(model_name="gpt-5-mini"),
        )

    assert exc_info.value.__cause__ is error
    stored = sqlite_instance.get_conversation_messages(conversation_id=conversation_id)
    assert [(m.get_piece().role, m.get_piece().response_error) for m in stored] == [
        ("user", "none"),
        ("assistant", "processing"),
    ]
    client.start.assert_awaited_once()
    client.stop.assert_awaited_once()
    client.get_session_metadata.assert_not_awaited()
    if failure_stage == "start":
        client.get_status.assert_not_awaited()
    else:
        client.get_status.assert_awaited_once()
    if failure_stage in ("start", "status"):
        client.create_session.assert_not_awaited()
        session.send_and_wait.assert_not_awaited()
        client.delete_session.assert_not_awaited()
    else:
        client.create_session.assert_awaited_once()
        session.send_and_wait.assert_awaited_once()
        client.delete_session.assert_awaited_once_with("sdk-session-id")
        session.on.return_value.assert_called_once_with()
    session.send.assert_not_awaited()


@pytest.mark.usefixtures("patch_central_database")
async def test_normalizer_surfaces_dispatch_timeout_and_cleans_up_without_replay_async(
    *,
    sdk: Any,
    client: NonCallableMagicMock,
) -> None:
    session = client.create_session.return_value

    async def stall_send_async(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.Event().wait()

    session.send.side_effect = stall_send_async
    session.send_and_wait.side_effect = partial(sdk.CopilotSession.send_and_wait, session)
    with pytest.raises(Exception, match="Error sending prompt with conversation ID:") as exc_info:
        # A bare watchdog TimeoutError must not satisfy the normalizer-wrapped failure.
        await asyncio.wait_for(
            PromptNormalizer().send_prompt_async(
                message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
                target=GitHubCopilotTarget(model_name="gpt-5-mini", response_timeout_seconds=0.01),
            ),
            timeout=2.0,
        )
    assert isinstance(exc_info.value.__cause__, TimeoutError)
    session.send.assert_awaited_once()
    client.delete_session.assert_awaited_once_with("sdk-session-id")
    client.stop.assert_awaited_once()


@pytest.mark.usefixtures("patch_central_database")
@pytest.mark.parametrize("invalid_reply", ["empty", "subagent", "absent", "non-assistant"])
async def test_normalizer_rejects_invalid_reply_async(
    *,
    client: NonCallableMagicMock,
    sqlite_instance: MemoryInterface,
    invalid_reply: str,
) -> None:
    from copilot.generated.session_events import SessionEventType, SessionIdleData

    reply = _assistant_reply("HELLO")
    session = client.create_session.return_value
    session.send_and_wait.return_value = {
        "empty": _assistant_reply(""),
        "subagent": replace(reply, agent_id="sdk-subagent-id"),
        "absent": None,
        "non-assistant": replace(reply, type=SessionEventType.SESSION_IDLE, data=SessionIdleData(aborted=False)),
    }[invalid_reply]
    conversation_id = str(uuid4())
    with pytest.raises(Exception, match="Error sending prompt with conversation ID:") as exc_info:
        await PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
            conversation_id=conversation_id,
            target=GitHubCopilotTarget(model_name="gpt-5-mini"),
        )
    assert isinstance(exc_info.value.__cause__, ValueError)
    stored = sqlite_instance.get_conversation_messages(conversation_id=conversation_id)
    assert [(m.get_piece().role, m.get_piece().response_error) for m in stored] == [
        ("user", "none"),
        ("assistant", "processing"),
    ]
    session.send_and_wait.assert_awaited_once()
    session.on.return_value.assert_called_once_with()
    client.delete_session.assert_awaited_once_with("sdk-session-id")
    client.stop.assert_awaited_once()


@pytest.mark.usefixtures("patch_central_database")
@pytest.mark.parametrize(
    ("event_stream", "expected_error"),
    [("aborted-idle", "abort"), ("abort-then-idle", "abort"), ("tool-then-idle", "tool")],
    ids=["aborted-idle", "abort-then-idle", "tool-then-idle"],
)
async def test_normalizer_rejects_unsafe_events_async(
    *,
    sdk: Any,
    client: NonCallableMagicMock,
    sqlite_instance: MemoryInterface,
    event_stream: str,
    expected_error: str,
) -> None:
    from copilot.generated.session_events import AbortData, SessionEventType, SessionIdleData, ToolExecutionStartData

    session = client.create_session.return_value
    reply = _assistant_reply("HELLO")
    events = {
        "aborted-idle": [
            reply,
            replace(reply, id=uuid4(), type=SessionEventType.SESSION_IDLE, data=SessionIdleData(aborted=True)),
        ],
        "abort-then-idle": [
            reply,
            replace(
                reply,
                id=uuid4(),
                type=SessionEventType.ABORT,
                data=AbortData.from_dict({"reason": "user_initiated"}),
            ),
            replace(reply, id=uuid4(), type=SessionEventType.SESSION_IDLE, data=SessionIdleData(aborted=None)),
        ],
        "tool-then-idle": [
            replace(
                reply,
                id=uuid4(),
                type=SessionEventType.TOOL_EXECUTION_START,
                data=ToolExecutionStartData(
                    tool_call_id="synthetic-tool-call", tool_name="benign_test_tool", arguments={"text": "HELLO"}
                ),
            ),
            reply,
            replace(reply, id=uuid4(), type=SessionEventType.SESSION_IDLE, data=SessionIdleData(aborted=False)),
        ],
    }[event_stream]
    handlers: list[Callable[[SessionEvent], None]] = []

    def subscribe(handler: Callable[[SessionEvent], None]) -> Callable[[], None]:
        handlers.append(handler)
        return partial(handlers.remove, handler)

    async def send_events_async(*_args: Any, **_kwargs: Any) -> str:
        for event in events:
            for handler in tuple(handlers):
                handler(event)
        return "sdk-request-id"

    session.on.side_effect = subscribe
    session.send.side_effect = send_events_async
    session.send_and_wait.side_effect = partial(sdk.CopilotSession.send_and_wait, session)
    conversation_id = str(uuid4())
    with pytest.raises(Exception, match="Error sending prompt with conversation ID:") as exc_info:
        await PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
            conversation_id=conversation_id,
            target=GitHubCopilotTarget(model_name="gpt-5-mini", response_timeout_seconds=1.0),
        )
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert expected_error in str(exc_info.value.__cause__).lower()
    stored = sqlite_instance.get_conversation_messages(conversation_id=conversation_id)
    assert [(m.get_piece().role, m.get_piece().response_error) for m in stored] == [
        ("user", "none"),
        ("assistant", "processing"),
    ]
    session.send.assert_awaited_once()
    client.delete_session.assert_awaited_once_with("sdk-session-id")
    client.stop.assert_awaited_once()
    assert not handlers


@pytest.mark.usefixtures("patch_central_database")
@pytest.mark.parametrize("retain_session", [False, True], ids=["delete", "retain"])
async def test_normalizer_cleans_up_partial_creation_async(
    *,
    client: NonCallableMagicMock,
    sqlite_instance: MemoryInterface,
    caplog: pytest.LogCaptureFixture,
    retain_session: bool,
) -> None:
    session = client.create_session.return_value
    sessions = {"unrelated-session-id"}
    allocated_session_id = ""
    creation_error = RuntimeError("Copilot post-create options update failed")

    async def create_session_async(*, session_id: str = "sdk-generated-session-id", **_kwargs: Any) -> None:
        nonlocal allocated_session_id
        allocated_session_id = session_id
        sessions.add(session_id)
        raise creation_error

    client.create_session.side_effect = create_session_async
    _mock_session_storage(client=client, sessions=sessions)
    conversation_id = str(uuid4())
    with caplog.at_level(logging.INFO, logger=TARGET_LOGGER):
        with pytest.raises(Exception, match="Error sending prompt with conversation ID:") as exc_info:
            await PromptNormalizer().send_prompt_async(
                message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
                conversation_id=conversation_id,
                target=GitHubCopilotTarget(model_name="gpt-5-mini", retain_session=retain_session),
            )

    assert exc_info.value.__cause__ is creation_error
    assert allocated_session_id == client.create_session.await_args.kwargs["session_id"]
    assert str(UUID(allocated_session_id)) == allocated_session_id
    assert allocated_session_id != "sdk-session-id"
    client.create_session.assert_awaited_once()
    client.get_session_metadata.assert_awaited_once_with(allocated_session_id)
    session.send_and_wait.assert_not_awaited()
    session.send.assert_not_awaited()
    client.stop.assert_awaited_once()
    stored = sqlite_instance.get_conversation_messages(conversation_id=conversation_id)
    assert [(m.get_piece().role, m.get_piece().response_error) for m in stored] == [
        ("user", "none"),
        ("assistant", "processing"),
    ]
    retained_logs = [
        r.getMessage()
        for r in caplog.records
        if r.name == TARGET_LOGGER
        and r.levelno == logging.INFO
        and r.getMessage().startswith("Retaining Copilot session ")
    ]
    if retain_session:
        client.delete_session.assert_not_awaited()
        assert sessions == {"unrelated-session-id", allocated_session_id}
        assert retained_logs == [f"Retaining Copilot session {allocated_session_id} as requested; delete it manually."]
    else:
        client.delete_session.assert_awaited_once_with(allocated_session_id)
        assert sessions == {"unrelated-session-id"}
        assert not retained_logs


@pytest.mark.usefixtures("patch_central_database")
async def test_normalizer_deletes_owned_session_when_creation_is_cancelled_after_allocation_async(
    client: NonCallableMagicMock,
) -> None:
    session = client.create_session.return_value
    sessions = {"unrelated-session-id"}
    allocated = asyncio.Event()

    async def create_session_async(*, session_id: str = "sdk-generated-session-id", **_kwargs: Any) -> None:
        sessions.add(session_id)
        allocated.set()
        await asyncio.Event().wait()

    client.create_session.side_effect = create_session_async
    _mock_session_storage(client=client, sessions=sessions)
    request_task = asyncio.create_task(
        PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
            target=GitHubCopilotTarget(model_name="gpt-5-mini"),
        )
    )
    try:
        await asyncio.wait_for(allocated.wait(), timeout=2.0)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task
    finally:
        if not request_task.done():
            request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)

    client.create_session.assert_awaited_once()
    allocated_id = client.create_session.await_args.kwargs["session_id"]
    client.get_session_metadata.assert_awaited_once_with(allocated_id)
    client.delete_session.assert_awaited_once_with(allocated_id)
    session.send_and_wait.assert_not_awaited()
    session.send.assert_not_awaited()
    client.stop.assert_awaited_once()
    assert sessions == {"unrelated-session-id"}


@pytest.mark.usefixtures("patch_central_database")
async def test_normalizer_stops_owned_client_when_startup_is_cancelled_async(
    *,
    sdk: Any,
    client: NonCallableMagicMock,
) -> None:
    session = client.create_session.return_value
    owned_resources: set[str] = set()
    allocated = asyncio.Event()
    original_cancellation: asyncio.CancelledError | None = None

    async def start_async() -> None:
        nonlocal original_cancellation
        owned_resources.add("owned-runtime")
        allocated.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            original_cancellation = error
            raise

    async def stop_async() -> None:
        owned_resources.remove("owned-runtime")

    client.start.side_effect = start_async
    client.stop.side_effect = stop_async
    request_task = asyncio.create_task(
        PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
            target=GitHubCopilotTarget(model_name="gpt-5-mini"),
        )
    )
    try:
        await asyncio.wait_for(allocated.wait(), timeout=2.0)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await request_task
    finally:
        if not request_task.done():
            request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)

    assert exc_info.value is original_cancellation
    sdk.CopilotClient.assert_called_once()
    client.start.assert_awaited_once()
    client.get_status.assert_not_awaited()
    client.create_session.assert_not_awaited()
    client.get_session_metadata.assert_not_awaited()
    client.delete_session.assert_not_awaited()
    session.send.assert_not_awaited()
    session.send_and_wait.assert_not_awaited()
    client.stop.assert_awaited_once()
    assert owned_resources == set()


@pytest.mark.usefixtures("patch_central_database")
@pytest.mark.parametrize(
    ("github_token", "use_working_directory", "max_requests_per_minute"),
    [
        pytest.param("  dummy-github-token  ", False, None, id="token-only"),
        pytest.param(None, True, None, id="directory-only"),
        pytest.param(None, False, 30, id="throttle-only"),
        pytest.param("  dummy-github-token  ", True, 30, id="all-options"),
    ],
)
async def test_normalizer_forwards_options_without_exposing_token_async(
    *,
    sdk: Any,
    client: NonCallableMagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    github_token: str | None,
    use_working_directory: bool,
    max_requests_per_minute: int | None,
) -> None:
    with (
        caplog.at_level(logging.DEBUG, logger=TARGET_LOGGER),
        patch.object(asyncio, "sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        target = GitHubCopilotTarget(
            model_name="gpt-5-mini",
            github_token=github_token,
            working_directory=tmp_path if use_working_directory else None,
            max_requests_per_minute=max_requests_per_minute,
        )
        response = await PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(), target=target
        )
    sdk.CopilotClient.assert_called_once_with(
        github_token=github_token, working_directory=str(tmp_path) if use_working_directory else None
    )
    assert response.get_piece().converted_value == "HELLO"
    client.create_session.return_value.send_and_wait.assert_awaited_once()
    if max_requests_per_minute is not None:
        mock_sleep.assert_awaited_once_with(2.0)
        assert target.get_identifier().params["max_requests_per_minute"] == 30
    else:
        mock_sleep.assert_not_awaited()
    if use_working_directory:
        assert target.get_identifier().params["working_directory"] == str(tmp_path)
    assert "dummy-github-token" not in target.get_identifier().model_dump_json()
    assert "dummy-github-token" not in caplog.text


def test_init_without_copilot_sdk_reports_installation_guidance() -> None:
    with patch.dict("sys.modules", {"copilot": None}):
        with pytest.raises(RuntimeError, match=r"pip install pyrit\[github-copilot\]"):
            GitHubCopilotTarget(model_name="gpt-5-mini")


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"model_name": "   "}, "model_name", id="blank-model"),
        pytest.param({"github_token": "   "}, "github_token", id="blank-token"),
        pytest.param({"response_timeout_seconds": 0}, "response_timeout_seconds", id="zero-timeout"),
        pytest.param({"response_timeout_seconds": -1}, "response_timeout_seconds", id="negative-timeout"),
        pytest.param({"response_timeout_seconds": float("inf")}, "response_timeout_seconds", id="infinite-timeout"),
        pytest.param({"response_timeout_seconds": float("nan")}, "response_timeout_seconds", id="nan-timeout"),
        pytest.param({"working_directory": "   "}, "working_directory", id="blank-directory"),
    ],
)
def test_init_rejects_invalid_options_before_sdk_import(*, overrides: dict[str, Any], field: str) -> None:
    with patch.dict("sys.modules", {"copilot": None}), pytest.raises(ValueError, match=field):
        GitHubCopilotTarget(**{"model_name": "gpt-5-mini", **overrides})


@pytest.mark.parametrize("path_kind", ["missing", "file"])
def test_init_rejects_non_directory_before_sdk_import(*, tmp_path: Path, path_kind: str) -> None:
    path = tmp_path / "not-a-directory"
    if path_kind == "file":
        path.write_text("local test fixture", encoding="utf-8")
    with patch.dict("sys.modules", {"copilot": None}), pytest.raises(ValueError, match="working_directory"):
        GitHubCopilotTarget(model_name="gpt-5-mini", working_directory=path)


@pytest.mark.usefixtures("patch_central_database")
async def test_normalizer_keeps_event_loop_responsive_during_client_construction_async(
    *, sdk: Any, client: NonCallableMagicMock, sqlite_instance: MemoryInterface
) -> None:
    loop = asyncio.get_running_loop()
    constructor_entered = asyncio.Event()
    constructor_finished = asyncio.Event()
    release = threading.Event()
    released_while_constructing = False

    def construct_client(*, github_token: str | None, working_directory: str | None) -> NonCallableMagicMock:
        nonlocal released_while_constructing
        try:
            loop.call_soon_threadsafe(constructor_entered.set)
            # The bound lets a blocked event loop escape without satisfying the responsiveness assertion.
            released_while_constructing = release.wait(timeout=5.0)
            return client
        finally:
            loop.call_soon_threadsafe(constructor_finished.set)

    async def release_constructor_async() -> None:
        await constructor_entered.wait()
        release.set()

    sdk.CopilotClient.side_effect = construct_client
    conversation_id = str(uuid4())
    request_task = asyncio.create_task(
        PromptNormalizer().send_prompt_async(
            message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
            conversation_id=conversation_id,
            target=GitHubCopilotTarget(model_name="gpt-5-mini"),
        )
    )
    release_task = asyncio.create_task(release_constructor_async())
    try:
        response, _ = await asyncio.wait_for(asyncio.gather(request_task, release_task), timeout=10.0)
    finally:
        release.set()
        for task in (request_task, release_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(request_task, release_task, return_exceptions=True)
        await asyncio.wait_for(constructor_finished.wait(), timeout=5.0)

    assert isinstance(response, Message)
    piece = response.get_piece()
    assert (piece.role, piece.converted_value, piece.conversation_id, piece.response_error) == (
        "assistant",
        "HELLO",
        conversation_id,
        "none",
    )
    stored = sqlite_instance.get_conversation_messages(conversation_id=conversation_id)
    assert [(m.get_piece().role, m.get_piece().converted_value, m.get_piece().response_error) for m in stored] == [
        ("user", "Reply exactly HELLO.", "none"),
        ("assistant", "HELLO", "none"),
    ]
    sdk.CopilotClient.assert_called_once_with(github_token=None, working_directory=None)
    client.start.assert_awaited_once()
    client.create_session.return_value.send_and_wait.assert_awaited_once_with("Reply exactly HELLO.", timeout=60.0)
    client.stop.assert_awaited_once()
    client.delete_session.assert_awaited_once_with("sdk-session-id")
    assert released_while_constructing is True
