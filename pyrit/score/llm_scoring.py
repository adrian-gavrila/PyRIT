# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pyrit.exceptions import (
    EmptyResponseException,
    InvalidJsonException,
    ScorerLLMResponseBlockedException,
    pyrit_json_retry,
)
from pyrit.models import Message, MessagePiece
from pyrit.prompt_normalizer import PromptNormalizer, send_json_with_retry_async

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyrit.models import (
        ComponentIdentifier,
        PromptDataType,
        UnvalidatedScore,
    )
    from pyrit.prompt_target import PromptTarget
    from pyrit.score.response_handler import ResponseHandler


async def _run_llm_scoring_async(
    *,
    chat_target: PromptTarget,
    system_prompt: str | None,
    response_handler: ResponseHandler,
    value: str,
    data_type: PromptDataType,
    scored_prompt_id: str | uuid.UUID,
    scorer_identifier: ComponentIdentifier,
    prepended_text: str | None = None,
    category: Sequence[str] | str | None = None,
    objective: str | None = None,
    normalizer: PromptNormalizer | None = None,
    fresh_conversation_per_attempt: bool = False,
) -> UnvalidatedScore:
    """
    Perform a single scoring round-trip against an LLM target and delegate parsing.

    This is the shared LLM evaluation mechanism: it optionally sets a system prompt on the target, sends
    the value to be scored (forwarding ``response_handler.json_response_config`` so targets that
    support structured output can enforce it), and delegates parsing and validation to
    ``response_handler``. The round-trip uses a ``PromptNormalizer`` so the scorer's question and
    the target's answer are persisted to memory (a full audit trail, and a real conversation an
    attack can link as a SCORE-type related conversation). The default editable-history path rolls
    memory back between JSON attempts; ``fresh_conversation_per_attempt`` keeps malformed judge
    exchanges in separate conversations instead of replaying native history. It is intentionally
    stateless and independent of any particular ``Scorer`` so scorers can compose it without
    inheriting LLM machinery.

    The round-trip owns only the transport; the ``ResponseHandler`` owns the response contract —
    the optional response schema and turning raw text into a validated ``UnvalidatedScore``.

    This function is intentionally module-internal (underscore-prefixed): it is a composition
    primitive with no public-API stability or deprecation contract. Scorers in this package call
    it directly; external callers should compose scorers rather than this helper.

    Args:
        chat_target (PromptTarget): The target LLM to send the message to.
        system_prompt (str | None): The system-level prompt that guides the target LLM. When None,
            the request is sent without configuring a system prompt.
        response_handler (ResponseHandler): Owns the response contract: supplies the optional
            response schema and turns the target's raw text into an ``UnvalidatedScore``.
        value (str): The content to be scored (e.g. text, image path, audio path).
        data_type (PromptDataType): The data type of ``value`` (e.g. "text", "image_path").
        scored_prompt_id (str | uuid.UUID): The ID of the message piece being scored.
        scorer_identifier (ComponentIdentifier): Identifier of the calling scorer, stored on
            the resulting score.
        prepended_text (str | None): Text context to prepend before ``value`` as a separate
            piece. Useful for adding objective/context when scoring non-text content.
            Defaults to None.
        category (Sequence[str] | str | None): The category of the score. May instead be parsed
            from the response; supplying both is an error. Defaults to None.
        objective (str | None): The objective associated with the score, used for
            contextualizing the result. Defaults to None.
        normalizer (PromptNormalizer | None): Normalizer used to send the scoring round-trip.
            Injectable for testing; defaults to a fresh ``PromptNormalizer()`` when not supplied.
        fresh_conversation_per_attempt (bool): Use a fresh conversation for each attempt when
            rolling back target history is not supported. Defaults to False.

    Returns:
        UnvalidatedScore: The parsed score, whose ``raw_score_value`` still needs to be
            normalized and validated by the caller.

    Raises:
        ScorerLLMResponseBlockedException: If the scorer's LLM response is blocked by
            content filtering. The transport only surfaces the condition; the calling
            ``Scorer`` owns the policy for whether to raise or return a default score.
        EmptyResponseException: If the scorer's LLM response has message pieces but none of them
            are text and none are blocked (a rare no-text-modality shape). Note a genuinely empty
            text reply does NOT surface here: the normalizer converts an empty target response into
            an empty text piece, which fails JSON parsing and is therefore retried and, if still
            empty, ultimately raised as ``InvalidJsonException``.
        InvalidJsonException: If the response is not valid JSON, is missing required keys, or
            fails the handler's value validation. This also covers an empty text reply (normalized
            to ``""``), which is retried before being surfaced here.
        Exception: For other unexpected errors during scoring.
    """
    conversation_id = str(uuid.uuid4())

    if system_prompt is not None and not fresh_conversation_per_attempt:
        chat_target.set_system_prompt(
            system_prompt=system_prompt,
            conversation_id=conversation_id,
        )
    # Forward the JSON-response request (format and any schema together) via the handler's
    # canonical config; the target's normalization pipeline omits the schema when it cannot
    # natively enforce one.
    prompt_metadata = response_handler.json_response_config.to_metadata()

    # Build message pieces - prepended text context first (if provided), then the main message being scored
    message_pieces: list[MessagePiece] = []

    # Add prepended text context piece if provided (e.g., objective context for non-text scoring)
    if prepended_text:
        message_pieces.append(
            MessagePiece(
                role="user",
                original_value=prepended_text,
                original_value_data_type="text",
                converted_value_data_type="text",
                conversation_id=conversation_id,
                prompt_metadata=prompt_metadata,
            )
        )

    # Add the main message piece being scored
    message_pieces.append(
        MessagePiece(
            role="user",
            original_value=value,
            original_value_data_type=data_type,
            converted_value_data_type=data_type,
            conversation_id=conversation_id,
            prompt_metadata=prompt_metadata,
        )
    )

    scorer_llm_request = Message(message_pieces=message_pieces)

    # Resolve the text piece that holds the JSON response (score_value + rationale). The normalizer
    # converts an empty or blocked-then-empty target response into an empty text piece, so a genuine
    # empty reply lands here as text_piece.converted_value == "" -> parse fails -> retried as invalid
    # JSON. The text_piece-is-None branch below therefore only fires for a response that has pieces
    # but no text piece: a content-filter block surfaces as its own exception (the calling Scorer
    # owns whether to raise or fall back), and any other no-text shape is a genuine empty/malformed
    # error. Neither of those is retried; only invalid JSON triggers a retry.
    def _parse(response: Message) -> UnvalidatedScore:
        text_piece = next(
            (piece for piece in response.message_pieces if piece.converted_value_data_type == "text"), None
        )
        if text_piece is None:
            if any(piece.is_blocked() for piece in response.message_pieces):
                raise ScorerLLMResponseBlockedException(
                    message=(
                        f"The scorer's LLM response was blocked by content filtering while scoring "
                        f"prompt ID: {scored_prompt_id}. Consider using a scorer endpoint with "
                        f"content filtering disabled for red-teaming workflows."
                    )
                )
            raise EmptyResponseException(
                message=(
                    f"The scorer's LLM response contained no text to parse while scoring prompt ID: {scored_prompt_id}."
                )
            )

        return response_handler.parse(
            response_text=text_piece.converted_value,
            scorer_identifier=scorer_identifier,
            scored_prompt_id=scored_prompt_id,
            category=category,
            objective=objective,
        )

    # Editable targets retry on a rolled-back conversation; non-editable judges opt into fresh sessions.
    try:
        if fresh_conversation_per_attempt:
            active_normalizer = normalizer or PromptNormalizer()
            first_attempt = True

            @pyrit_json_retry
            async def _fresh_attempt_async() -> UnvalidatedScore:
                nonlocal first_attempt
                attempt_conversation_id = conversation_id if first_attempt else str(uuid.uuid4())
                attempt_message = scorer_llm_request if first_attempt else scorer_llm_request.duplicate()
                if system_prompt is not None:
                    chat_target.set_system_prompt(
                        system_prompt=system_prompt,
                        conversation_id=attempt_conversation_id,
                    )
                first_attempt = False
                response = await active_normalizer.send_prompt_async(
                    message=attempt_message,
                    conversation_id=attempt_conversation_id,
                    target=chat_target,
                )
                if not response:
                    raise ValueError(f"No response received for conversation ID: {attempt_conversation_id}")
                try:
                    return _parse(response)
                except InvalidJsonException:
                    try:
                        await chat_target.reset_conversation_async(conversation_id=attempt_conversation_id)
                    except Exception as reset_error:
                        raise RuntimeError(
                            "Could not release the malformed judge session; refusing another retry."
                        ) from reset_error
                    raise

            fresh_score: UnvalidatedScore = await _fresh_attempt_async()
            return fresh_score

        return await send_json_with_retry_async(
            normalizer=normalizer or PromptNormalizer(),
            target=chat_target,
            message=scorer_llm_request,
            conversation_id=conversation_id,
            parse=_parse,
        )
    except (ScorerLLMResponseBlockedException, EmptyResponseException, InvalidJsonException):
        # Terminal / caller-owned outcomes: propagate unchanged so the calling Scorer can apply
        # its own policy (fall back, raise, or -- for invalid JSON -- surface the retry exhaustion).
        raise
    except Exception as ex:
        raise Exception(f"Error scoring prompt with original prompt ID: {scored_prompt_id}") from ex
