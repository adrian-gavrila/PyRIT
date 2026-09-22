# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import asyncio
import logging
import math
from pathlib import Path
from uuid import uuid4

from pyrit.models import ComponentIdentifier, Message, construct_response_from_request
from pyrit.prompt_target.common.prompt_target import PromptTarget
from pyrit.prompt_target.common.utils import limit_requests_per_minute

logger = logging.getLogger(__name__)


class GitHubCopilotTarget(PromptTarget):
    """
    Send single-turn text requests through the GitHub Copilot SDK.

    Capture INFO logs for session mapping and SDK/runtime version diagnostics.
    """

    def __init__(
        self,
        *,
        model_name: str,
        github_token: str | None = None,
        working_directory: str | Path | None = None,
        retain_session: bool = False,
        response_timeout_seconds: float = 60.0,
        max_requests_per_minute: int | None = None,
    ) -> None:
        """
        Initialize the target with an explicit token or normal SDK login discovery.

        Args:
            model_name (str): Explicit Copilot model ID.
            github_token (str | None): Nonblank GitHub token, forwarded unchanged with precedence over other
                authentication methods. Defaults to None for SDK environment-token and saved-login discovery.
            working_directory (str | Path | None): Existing local directory; no Git repository is required.
                Supplied paths are resolved once against the current directory at construction and included
                in saved target identifiers. Defaults to None for the SDK's current directory at each client start.
            retain_session (bool): Keep each Copilot session on disk instead of deleting it. Retained
                session IDs are logged so they can be found later. Defaults to False.
            response_timeout_seconds (float): Shared time budget for dispatch and completion, in seconds.
                Excludes client/session creation, startup status lookup, and cleanup. Defaults to 60.
            max_requests_per_minute (int | None): PyRIT per-send pacing. Positive values delay each send by
                60 / value seconds before SDK client creation, outside the response deadline.
                None or nonpositive values disable pacing. Defaults to None.

        Raises:
            ValueError: If model_name or a supplied github_token is blank, or response_timeout_seconds
                is not finite and positive, or a supplied working_directory is blank, missing, or not a directory.
            OSError: If the working directory cannot be resolved or inspected.
            RuntimeError: If the optional GitHub Copilot SDK is not installed.
        """
        if not model_name.strip():
            raise ValueError("model_name must not be empty.")
        if github_token is not None and not github_token.strip():
            raise ValueError("github_token must not be blank when supplied.")
        if not math.isfinite(response_timeout_seconds) or response_timeout_seconds <= 0:
            raise ValueError("response_timeout_seconds must be a finite positive number.")

        self._working_directory: str | None = None
        if working_directory is not None:
            if isinstance(working_directory, str) and not working_directory.strip():
                raise ValueError("working_directory must not be blank when supplied.")
            resolved_directory = Path(working_directory).resolve()
            if not resolved_directory.is_dir():
                raise ValueError("working_directory must be an existing directory.")
            self._working_directory = str(resolved_directory)

        try:
            import copilot
        except ModuleNotFoundError as e:
            raise RuntimeError("Could not import copilot. Install it with 'pip install pyrit[github-copilot]'.") from e

        super().__init__(model_name=model_name, max_requests_per_minute=max_requests_per_minute)
        self._sdk = copilot
        self._github_token = github_token
        self._retain_session = retain_session
        self._response_timeout_seconds = response_timeout_seconds

    def _build_identifier(self) -> ComponentIdentifier:
        """
        Build the identifier with the selected working directory.

        Returns:
            ComponentIdentifier: The target identifier, including the resolved selected path when provided.
        """
        return self._create_identifier(params={"working_directory": self._working_directory})

    @limit_requests_per_minute
    async def _send_prompt_to_target_async(self, *, normalized_conversation: list[Message]) -> list[Message]:
        request = normalized_conversation[-1].get_piece()
        reply_text = await self._send_text_async(
            prompt=request.converted_value, conversation_id=request.conversation_id
        )
        return [construct_response_from_request(request=request, response_text_pieces=[reply_text])]

    async def _send_text_async(self, *, prompt: str, conversation_id: str | None) -> str:
        from copilot.generated.session_events import (
            AbortData,
            AssistantMessageData,
            SessionEvent,
            SessionIdleData,
            ToolExecutionStartData,
        )

        aborted = False
        tool_execution_started = False

        def _record_turn_events(event: SessionEvent) -> None:
            nonlocal aborted, tool_execution_started
            if isinstance(event.data, AbortData) or (isinstance(event.data, SessionIdleData) and event.data.aborted):
                aborted = True
            if isinstance(event.data, ToolExecutionStartData):
                tool_execution_started = True

        client = await asyncio.to_thread(
            self._sdk.CopilotClient, github_token=self._github_token, working_directory=self._working_directory
        )
        try:
            await client.start()
            status = await client.get_status()
            session_id = str(uuid4())
            logger.info(
                "Attempting Copilot session creation: pyrit_conversation_id=%s requested_sdk_session_id=%s "
                "sdk_version=%s runtime_version=%s runtime_protocol_version=%s retain_session=%s remote_mode=OFF",
                conversation_id,
                session_id,
                self._sdk.__version__,
                status.version,
                status.protocol_version,
                self._retain_session,
            )
            try:
                # SDK runtime instructions are retained; other SDK-provided context may remain.
                session = await client.create_session(
                    session_id=session_id,
                    model=self._model_name,
                    system_message={
                        "mode": "customize",
                        "sections": {
                            "environment_context": {"action": "remove"},
                            "custom_instructions": {"action": "remove"},
                        },
                    },
                    remote_session=self._sdk.RemoteSessionMode.OFF,
                    available_tools=[],
                    skip_custom_instructions=True,
                    instruction_directories=[],
                    enable_host_git_operations=False,
                    enable_config_discovery=False,
                    organization_custom_instructions="",
                    enable_on_demand_instruction_discovery=False,
                    infinite_sessions={"enabled": False},
                    memory={"enabled": False},
                    enable_session_store=False,
                    enable_file_hooks=False,
                )
            except (Exception, asyncio.CancelledError):
                if await client.get_session_metadata(session_id) is not None:
                    if self._retain_session:
                        logger.info("Retaining Copilot session %s as requested; delete it manually.", session_id)
                    else:
                        await client.delete_session(session_id)
                raise
            try:
                unsubscribe = session.on(_record_turn_events)
                try:
                    reply = await asyncio.wait_for(
                        session.send_and_wait(prompt, timeout=self._response_timeout_seconds),
                        timeout=self._response_timeout_seconds,
                    )
                finally:
                    unsubscribe()

                if aborted:
                    raise RuntimeError("Copilot turn was aborted.")
                if tool_execution_started:
                    raise RuntimeError("Copilot turn reported tool execution.")
                if (
                    reply is None
                    or reply.agent_id is not None
                    or not isinstance(reply.data, AssistantMessageData)
                    or not isinstance(reply.data.content, str)
                    or not reply.data.content
                ):
                    raise ValueError("Copilot did not return a non-empty root assistant text reply.")
                return reply.data.content
            finally:
                if self._retain_session:
                    logger.info("Retaining Copilot session %s as requested; delete it manually.", session.session_id)
                else:
                    await client.delete_session(session.session_id)
        finally:
            await client.stop()
