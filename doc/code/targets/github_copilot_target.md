# GitHub Copilot SDK target

`GitHubCopilotTarget` sends a fresh single-turn text exchange through the GitHub Copilot SDK.
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

## One text exchange

Run this standalone script from your chosen existing local directory; no Git checkout is needed.
`Path.cwd()` explicitly selects that directory, and its resolved path is included in the target
identity. Running the script makes a real Copilot model request.

```python
import asyncio
import logging
from pathlib import Path

from pyrit.models import MessagePiece
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
    response = await PromptNormalizer().send_prompt_async(
        message=MessagePiece(role="user", original_value="Reply exactly HELLO.").to_message(),
        target=target,
    )
    print(response.get_piece().converted_value)


if __name__ == "__main__":
    asyncio.run(main_async())
```

The normalizer owns conversion and request/response persistence. Here, [IN_MEMORY](../memory/0_memory.md)
keeps PyRIT records only for the current process, independently of Copilot's local session data.

## Retention and boundaries

By default, each exchange releases owned SDK resources and deletes its owned local SDK session data.
Set `retain_session=True` to keep that data for diagnostics; client cleanup still runs.
Cleanup errors propagate, and crashes can leave residual data. Remote session export is explicitly
`OFF` in either mode: local retention does not enable export, and `OFF` does not mean offline inference.

Capture the target's INFO logs for creation-attempt records linking PyRIT conversation IDs to requested
SDK session IDs, with SDK/runtime versions, protocol, retention and remote mode. These per-exchange
details are not automatically added to response metadata or target-identity exports. An attempt record
does not prove session allocation or a successful exchange; retained-session logs identify data kept
for diagnostics.

This target supports fresh single-turn text only, not a public caller system prompt or native
continuation. It requests text-only runtime restrictions and retains restricted SDK base instructions;
other SDK/runtime context may remain. These controls are not an OS sandbox.

`response_timeout_seconds` bounds dispatch and completion, excluding client/session startup, the
startup status lookup, PyRIT pacing and cleanup. A failed status lookup stops the exchange before
session creation or prompt dispatch. Errors, timeouts and cancellation propagate without automatic
replay or a rollback guarantee.
