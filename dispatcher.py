"""
Dispatch a Claude agent to work on an issue inside its repo_path.
Yields SSE-formatted strings so Flask can stream progress to the client.
"""

import json
import queue
import threading
from typing import Generator

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from notify import notify_discord


def _build_prompt(issue: dict, extra: str = "", max_turns: int | None = None) -> str:
    prompt = f"Work on the following issue:\n\n**{issue['title']}**"
    if issue.get("description"):
        prompt += f"\n\n{issue['description']}"

    number = issue.get("number")
    closes_line = f" Include a line 'Closes #{number}' in the PR body." if number else ""
    branch_hint = f"agent/issue-{number}" if number else "agent/<short-slug-of-the-issue>"
    prompt += (
        "\n\nWhen you are done, open a pull request with your changes instead of leaving them "
        "uncommitted or on the current branch:\n"
        f"1. Create and check out a new branch (e.g. `{branch_hint}`) off the current branch.\n"
        "2. Commit your changes with a descriptive message.\n"
        "3. Push the branch to the remote.\n"
        f"4. Open a pull request with `gh pr create`, with a title and body summarizing the change."
        f"{closes_line}\n"
        "Do not commit or push directly to the current branch."
    )

    if max_turns:
        prompt += (
            f"\n\nYou have a hard budget of about {max_turns} turns for this whole task, after "
            "which you will be cut off mid-work with nothing saved. If the issue turns out to be "
            "larger than expected, don't try to do everything: prioritize getting a working, "
            "reviewable slice committed, pushed, and opened as a draft PR (title prefixed 'WIP:') "
            "well before you run out of turns, with a comment on the PR listing what's left. A "
            "small working PR beats running out of turns with nothing pushed."
        )

    if extra:
        prompt += f"\n\n{extra}"
    return prompt


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def dispatch(
    issue: dict,
    extra_instructions: str = "",
    max_turns: int = 30,
    max_budget_usd: float | None = None,
) -> Generator[str, None, None]:
    """
    Run the agent on *issue* and yield SSE strings.
    Streams: {'type': 'text', 'text': '...'}
    Terminates with: {'type': 'result', 'result': '...', 'stop_reason': '...'}
    Errors yield: {'type': 'error', 'message': '...'}
    """
    prompt = _build_prompt(issue, extra_instructions, max_turns=max_turns)
    cwd = issue.get("repo_path")

    msg_queue: queue.Queue = queue.Queue()

    def run_agent() -> None:
        async def _run() -> None:
            try:
                async for message in query(
                    prompt=prompt,
                    options=ClaudeAgentOptions(
                        cwd=cwd,
                        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
                        permission_mode="bypassPermissions",
                        max_turns=max_turns,
                        max_budget_usd=max_budget_usd,
                        system_prompt={"type": "preset", "preset": "claude_code"},
                    ),
                ):
                    msg_queue.put(("msg", message))
                msg_queue.put(("done", None))
            except Exception as exc:
                msg_queue.put(("error", str(exc)))

        anyio.run(_run)

    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    while True:
        kind, payload = msg_queue.get()

        if kind == "done":
            break

        if kind == "error":
            yield _sse({"type": "error", "message": payload})
            notify_discord(f"❌ Agent failed on **{issue.get('title', 'issue')}**: {payload}")
            break

        message = payload
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text:
                    yield _sse({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    yield _sse({"type": "tool_use", "name": block.name, "input": block.input})

        elif isinstance(message, ResultMessage):
            yield _sse({
                "type": "result",
                "result": message.result,
                "stop_reason": message.stop_reason,
            })
            notify_discord(
                f"✅ Agent finished **{issue.get('title', 'issue')}** ({message.stop_reason})"
            )

    thread.join(timeout=5)
