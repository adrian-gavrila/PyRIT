# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import logging

from pyrit.models import Message, construct_response_from_request
from pyrit.prompt_target.common.prompt_target import PromptTarget

logger = logging.getLogger(__name__)


class GitHubCopilotTarget(PromptTarget):
    """Send single-turn text requests through the GitHub Copilot SDK."""

    def __init__(self, *, model_name: str, retain_session: bool = False) -> None:
        """
        Initialize the target with normal SDK login discovery.

        Args:
            model_name (str): Explicit Copilot model ID.
            retain_session (bool): Keep each Copilot session on disk instead of deleting it. Retained
                session IDs are logged so they can be found later. Defaults to False.
        """
        import copilot

        super().__init__(model_name=model_name)
        self._sdk = copilot
        self._retain_session = retain_session

    async def _send_prompt_to_target_async(self, *, normalized_conversation: list[Message]) -> list[Message]:
        request = normalized_conversation[-1].get_piece()
        reply_text = await self._send_text_async(request.converted_value)
        return [construct_response_from_request(request=request, response_text_pieces=[reply_text])]

    async def _send_text_async(self, prompt: str) -> str:
        from copilot.generated.session_events import AssistantMessageData

        async with self._sdk.CopilotClient() as client:
            # Keep the exchange local and free of ambient context so replies reflect the model, not the host machine.
            session = await client.create_session(
                model=self._model_name,
                remote_session=self._sdk.RemoteSessionMode.OFF,
                available_tools=[],
                skip_custom_instructions=True,
                instruction_directories=[],
                enable_host_git_operations=False,
                enable_config_discovery=False,
                organization_custom_instructions="",
                enable_on_demand_instruction_discovery=False,
            )
            try:
                reply = await session.send_and_wait(prompt)
                if (
                    reply is None
                    or not isinstance(reply.data, AssistantMessageData)
                    or not isinstance(reply.data.content, str)
                ):
                    raise ValueError("Copilot did not return an assistant text reply.")
                return reply.data.content
            finally:
                if self._retain_session:
                    logger.info("Retaining Copilot session %s as requested; delete it manually.", session.session_id)
                else:
                    await client.delete_session(session.session_id)
