import json
import subprocess
import uuid

import anthropic

from db import upsert_session
from issues import create_issue, delete_issue, update_issue

_client = anthropic.Anthropic()

BOARD_TOOLS = [
    {
        "name": "move_card",
        "description": "Move an issue to a different column on the board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issueId": {
                    "type": "string",
                    "description": "The numeric issue ID as a string, e.g. '3'",
                },
                "columnId": {
                    "type": "string",
                    "enum": ["backlog", "in-progress", "review", "done"],
                    "description": "Target column",
                },
            },
            "required": ["issueId", "columnId"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create a new issue on the board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "description": {"type": "string"},
                "columnId": {
                    "type": "string",
                    "enum": ["backlog", "in-progress", "review", "done"],
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_issue",
        "description": "Update the title, description, or status of an existing issue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issueId":     {"type": "string", "description": "Numeric issue ID as a string"},
                "title":       {"type": "string"},
                "description": {"type": "string"},
                "columnId": {
                    "type": "string",
                    "enum": ["backlog", "in-progress", "review", "done"],
                },
            },
            "required": ["issueId"],
        },
    },
    {
        "name": "delete_issue",
        "description": "Permanently delete an issue from the board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issueId": {"type": "string", "description": "Numeric issue ID as a string"},
            },
            "required": ["issueId"],
        },
    },
    {
        "name": "list_github_issues",
        "description": (
            "List issues from a GitHub repository using the gh CLI. "
            "Returns issue number, title, state, labels, author, and body."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub repo in owner/repo format",
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Filter by issue state (default: open)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max issues to return (default: 20)",
                },
            },
            "required": ["repo"],
        },
    },
    {
        "name": "import_github_issue",
        "description": (
            "Import a GitHub issue onto the board. Provide the repo and issue number. "
            "The issue will be created on the board with its title and body."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub repo in owner/repo format",
                },
                "issueNumber": {
                    "type": "integer",
                    "description": "The GitHub issue number to import",
                },
                "columnId": {
                    "type": "string",
                    "enum": ["backlog", "in-progress", "review", "done"],
                    "description": "Board column to place the issue in (default: backlog)",
                },
            },
            "required": ["repo", "issueNumber"],
        },
    },
]


def _gh_issue_list(repo: str, state: str = "open", limit: int = 20) -> list[dict]:
    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--json", "number,title,state,body,author,labels",
        "--limit", str(limit),
        "--state", state,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def _gh_issue_view(repo: str, number: int) -> dict:
    cmd = [
        "gh", "issue", "view", str(number),
        "--repo", repo,
        "--json", "number,title,state,body,author,labels",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def _build_system(tasks: list[dict], repo: str = "") -> str:
    if tasks:
        lines = [f"  #{t['id']}: {t['title']} [{t.get('columnId', '?')}]" for t in tasks]
        board_context = "Current board:\n" + "\n".join(lines)
    else:
        board_context = "The board has no issues yet."

    repo_context = f"\nThe board is showing issues from GitHub repo: {repo}" if repo else ""

    return (
        "You are a project-management assistant for a Kanban board backed by GitHub issues. "
        "The board displays issues from a GitHub repository. "
        "Use list_github_issues to look up issues from the repo. "
        "Use the provided tools to move cards between columns when the user asks. "
        "Issue IDs on the board correspond to GitHub issue numbers. "
        "Always reply with a short, friendly confirmation after acting.\n\n"
        + board_context
        + repo_context
    )


def _handle_tool(name: str, inp: dict) -> tuple[str, dict | None]:
    """Execute a tool call. Returns (result_text, action_or_None)."""
    if name == "move_card":
        action = {
            "type":     "move_card",
            "issueId":  str(inp["issueId"]),
            "columnId": inp["columnId"],
        }
        return f"Moved #{inp['issueId']} to {inp['columnId']}.", action

    if name == "create_issue":
        issue = create_issue(
            title=inp["title"],
            description=inp.get("description", ""),
            status=inp.get("columnId", "todo"),
        )
        action = {
            "type":        "create_issue",
            "issueId":     str(issue["id"]),
            "title":       issue["title"],
            "description": issue.get("description", ""),
            "columnId":    inp.get("columnId", "todo"),
        }
        return f"Created issue #{issue['id']}: {issue['title']}.", action

    if name == "update_issue":
        issue = update_issue(
            int(inp["issueId"]),
            title=inp.get("title"),
            description=inp.get("description"),
            status=inp.get("columnId"),
        )
        if issue is None:
            return f"Issue #{inp['issueId']} not found.", None
        action = {
            "type":     "update_issue",
            "issueId":  str(issue["id"]),
            "title":    issue["title"],
            "columnId": issue.get("status"),
        }
        return f"Updated issue #{issue['id']}.", action

    if name == "delete_issue":
        deleted = delete_issue(int(inp["issueId"]))
        if not deleted:
            return f"Issue #{inp['issueId']} not found.", None
        action = {"type": "delete_issue", "issueId": str(inp["issueId"])}
        return f"Deleted issue #{inp['issueId']}.", action

    if name == "list_github_issues":
        try:
            issues = _gh_issue_list(
                inp["repo"],
                state=inp.get("state", "open"),
                limit=inp.get("limit", 20),
            )
        except Exception as exc:
            return f"Error fetching GitHub issues: {exc}", None
        summaries = []
        for iss in issues:
            labels = ", ".join(l["name"] for l in (iss.get("labels") or []))
            line = f"#{iss['number']} [{iss['state']}] {iss['title']}"
            if labels:
                line += f" ({labels})"
            summaries.append(line)
        return "\n".join(summaries) if summaries else "No issues found.", None

    if name == "import_github_issue":
        try:
            gh_issue = _gh_issue_view(inp["repo"], inp["issueNumber"])
        except Exception as exc:
            return f"Error fetching GitHub issue: {exc}", None
        col = inp.get("columnId", "backlog")
        issue = create_issue(
            title=f"#{gh_issue['number']} {gh_issue['title']}",
            description=gh_issue.get("body") or "",
            status=col,
            repo_name=inp["repo"],
            repo_url=f"https://github.com/{inp['repo']}",
        )
        action = {
            "type":        "create_issue",
            "issueId":     str(issue["id"]),
            "title":       issue["title"],
            "description": issue.get("description", ""),
            "columnId":    col,
        }
        return f"Imported GitHub #{gh_issue['number']} as board issue #{issue['id']}.", action

    return "Done.", None


def run_chat(message: str, tasks: list[dict], agent_id: str | None = None, repo: str = "") -> dict:
    """
    Run one chat turn with the board agent.

    Returns:
        { agent_id, message, actions }
    """
    agent_id = agent_id or str(uuid.uuid4())
    upsert_session(agent_id, "active")

    messages = [{"role": "user", "content": message}]
    actions: list[dict] = []
    reply = ""

    try:
        while True:
            response = _client.messages.create(
                model="claude-opus-4-6",
                max_tokens=1024,
                system=_build_system(tasks, repo),
                tools=BOARD_TOOLS,
                messages=messages,
            )

            tool_results = []
            for block in response.content:
                if block.type == "text":
                    reply = block.text

                elif block.type == "tool_use":
                    result_text, action = _handle_tool(block.name, block.input)
                    if action:
                        actions.append(action)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result_text,
                    })

            if response.stop_reason == "end_turn":
                break

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user",      "content": tool_results})

        upsert_session(agent_id, "idle")

    except Exception:
        upsert_session(agent_id, "error")
        raise

    return {"agent_id": agent_id, "message": reply or "Done!", "actions": actions}
