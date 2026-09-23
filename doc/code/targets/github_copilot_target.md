# GitHub Copilot SDK target

`GitHubCopilotTarget` sends text conversations through the GitHub Copilot SDK, retaining native
conversation state across turns.
Use Python 3.11 through 3.14 and install the optional extra:

```console
python -m pip install "pyrit[github-copilot]"
```

Authenticate beforehand with an eligible Copilot login, or supply an SDK environment token.
The SDK checks `COPILOT_GITHUB_TOKEN`, then `GH_TOKEN`, then `GITHUB_TOKEN`; environment tokens
take precedence over saved login. Alternatively, pass a securely supplied token as
`github_token=token`, which takes precedence over discovery. Do not hard-code credentials.
The target does not initiate interactive sign-in. See [PyRIT setup](../setup/0_setup.md)
for framework initialization.

## Two-turn native conversation

Run this standalone script from your chosen existing local directory; no Git checkout is needed.
`Path.cwd()` explicitly selects that directory, and its resolved path is included in the target
identity. Running the script makes **two real Copilot model requests**.

```python
import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from pyrit.models import Message
from pyrit.prompt_normalizer import PromptNormalizer
from pyrit.prompt_target import GitHubCopilotTarget
from pyrit.setup import IN_MEMORY, initialize_pyrit_async


async def main_async() -> None:
    await initialize_pyrit_async(memory_db_type=IN_MEMORY, silent=True)

    copilot_logger = logging.getLogger("pyrit.prompt_target.github_copilot_target")
    copilot_logger.setLevel(logging.INFO)
    copilot_logger.addHandler(logging.StreamHandler())
    copilot_logger.propagate = False

    target = GitHubCopilotTarget(
        model_name="gpt-5-mini",
        working_directory=Path.cwd(),
    )
    normalizer = PromptNormalizer()
    try:
        conversation_id = str(uuid4())
        target.set_system_prompt(system_prompt="Answer concisely.", conversation_id=conversation_id)

        for prompt in (
            "Remember the codeword ORCHID for this conversation.",
            "What codeword did I ask you to remember?",
        ):
            response = await normalizer.send_prompt_async(
                message=Message.from_prompt(prompt=prompt, role="user"),
                conversation_id=conversation_id,
                target=target,
            )
            print(response.get_piece().converted_value)
    finally:
        await target.cleanup_target_async()


if __name__ == "__main__":
    asyncio.run(main_async())
```

The normalizer owns conversion and request/response persistence. Here, [IN_MEMORY](../memory/0_memory.md)
keeps PyRIT records only for the current process, independently of Copilot's local session data.
The target lazily shares one client across isolated native conversations, with one native session per
PyRIT conversation; this shares SDK runtime/authentication, not process or security isolation. Keep
the target alive for all sends and join workflow tasks before calling `cleanup_target_async()`.

## Retention and boundaries

`cleanup_target_async()` is caller-owned, terminal cleanup. It drains active target work, rejects
new or queued target work, releases owned resources, and surfaces cleanup failures. It does not
gather caller tasks that may themselves be waiting on cleanup. By default, `retain_session=False`
deletes owned native session data. With `retain_session=True`, native session data is retained
but live session resources are still disconnected and the shared client is stopped. PyRIT memory
records remain independently retained. Crashes can still leave residual data. Remote session export
is explicitly `OFF` in either mode: local retention does not enable export, and `OFF` does not mean
offline inference.

`reset_conversation_async()` releases one established conversation without stopping unrelated
conversations or the shared client. A reset conversation is terminal: later sends with that
conversation ID fail rather than silently reopening native history. Unknown IDs and repeated
resets are safe no-ops. To start a new conversation, use a new PyRIT conversation ID; this target
does not resume, import, replay, fork, or reconcile native history. Edits to PyRIT memory do not
update the native Copilot context.

Capture the target's INFO logs for session-creation attempt records linking PyRIT conversation IDs
to requested SDK session IDs, with SDK/runtime versions, protocol, retention and remote mode. These
records are emitted before each native session creation attempt, not on every turn, and are not
automatically added to response metadata or target-identity exports. An attempt record does not
prove session allocation or a successful exchange; retained-session logs identify data kept for
diagnostics.

Each turn sends only the newest normalized user content; prior turns remain in the native session.
The target supports native continuation for text conversations and does not support editable history.

If a caller supplies an initial system prompt before the first user turn, the target uses SDK
replacement mode with that exact text. Replacement replaces the SDK's default system message and
its guardrails/security restrictions. The target still explicitly requests remote `OFF`, an empty
tool list, disabled configuration and instruction discovery, disabled host Git operations, session
store, memory, and file hooks. Without a supplied system prompt, the target keeps the existing
customize mode and removes only `environment_context` and `custom_instructions`; other SDK/runtime
context may remain. These controls are not an OS sandbox. Later public system-prompt changes are
rejected.

Finalize `model_name` before the first identity-capturing operation: `get_identifier()`, system
prompt setup, normalizer registration, scenario or registry use, target mapping, or direct native
execution. After identity capture, a different model requires a new target; setting the same model
again is harmless. Native SDK model switching is not used.

`response_timeout_seconds` bounds dispatch and completion, excluding client/session startup, the
startup status lookup, PyRIT pacing and cleanup. The synchronous SDK client constructor is moved
off the event loop, but cancellation cannot abort synchronous work already running in its worker.
A failed status lookup stops the exchange before session creation or prompt dispatch.

SDK-observed ambiguous timeout, cancellation, abort, unsafe event, or invalid native outcomes retire
only the affected conversation. Later sends to that ID fail, while a fresh ID can proceed; there is
no automatic replay. Errors, timeouts, and cancellation propagate without a crash-proof cleanup
guarantee. Downstream converter or PyRIT persistence failures occur outside the target's native
observation boundary and do not provide a safe-continuation guarantee.
