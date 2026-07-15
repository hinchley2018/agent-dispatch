"""
Polls GitHub issues for a repo and dispatches the Claude agent on any that
haven't been dispatched yet. Meant to be run on a schedule (cron/systemd timer),
not through Flask.

Dedup strategy: after an issue is dispatched, this script labels it via the gh
CLI — "agent-dispatched" on success, "agent-incomplete" if the agent errored
out or hit its turn/budget cap without finishing — and skips any issue that
already carries either label on future runs.

Ordering: issues are processed by milestone, lowest phase first, based on
milestone titles of the form "Phase N: <name>" (e.g. "Phase 0: Foundation").
Issues with no milestone or a non-matching milestone title are processed last.

Scope check: before running the (expensive) coding agent, a cheap classification
call judges whether the issue is actually scoped for one PR. If not, it's labeled
"needs-splitting" (skipped on future runs) instead of being dispatched.

Repo cleanliness: repo_path is force-reset to a clean copy of DISPATCH_BASE_BRANCH
(default "main") before and after every issue, so a crashed or failed attempt on
one issue never leaves uncommitted changes or a stray branch for the next issue.

Required env vars (see backend/.env):
  DISPATCH_REPO       owner/repo, e.g. "code-nurturers/saywork-platform"
  DISPATCH_REPO_PATH  local path to the repo checkout on this machine

Optional env vars:
  DISPATCH_LABEL       tracking label added once dispatched successfully (default: agent-dispatched)
  DISPATCH_FAIL_LABEL  label added when the agent errors out or hits its turn/budget cap
                       without finishing (default: agent-incomplete). Issues carrying
                       either label are skipped on future runs, so a failed issue gets
                       flagged for a human instead of being silently retried forever or
                       silently marked as done.
  DISPATCH_MAX_ISSUES  max issues to dispatch per run (default: 3)
  DISPATCH_STATE       issue state to poll: open|closed|all (default: open)
  DISPATCH_INSTRUCTIONS  extra instructions appended to every issue's prompt
  DISPATCH_MAX_TURNS      max agent turns per issue (default: 60)
  DISPATCH_MAX_BUDGET_USD max USD spend per issue, agent stops early if hit (default: 5.0)
  DISPATCH_SPLIT_LABEL    label added when an issue is judged too large to dispatch
                          (default: needs-splitting)
  DISPATCH_SCOPE_CHECK    set to "false" to skip the pre-dispatch scope check (default: true)
  DISPATCH_BASE_BRANCH    branch repo_path is reset to before/after each issue (default: main)
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys

import anthropic
from dotenv import load_dotenv

from dispatcher import _build_prompt, dispatch

load_dotenv()

_client = anthropic.Anthropic()

_SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "too_large": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["too_large", "reason"],
    "additionalProperties": False,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cron_dispatch")

_PHASE_RE = re.compile(r"^Phase\s+(\d+)\s*:", re.IGNORECASE)


def _phase_sort_key(issue: dict) -> tuple[int, int]:
    """Sort by milestone 'Phase N: ...' ascending, then issue number.
    Issues with no milestone or a non-matching title sort after all phases."""
    milestone = issue.get("milestone") or {}
    match = _PHASE_RE.match(milestone.get("title", ""))
    phase = int(match.group(1)) if match else float("inf")
    return (phase, issue["number"])


def _gh(args: list[str]) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def _reset_repo(repo_path: str, base_branch: str) -> None:
    """Discard any uncommitted changes or stray branches left behind by a
    previous (possibly crashed or failed) issue, so each issue starts from a
    clean copy of base_branch. Never touches remote history — only the local
    working tree and local branch pointer."""
    for args in (
        ["-C", repo_path, "fetch", "origin", base_branch],
        ["-C", repo_path, "checkout", "-f", base_branch],
        ["-C", repo_path, "reset", "--hard", f"origin/{base_branch}"],
        ["-C", repo_path, "clean", "-fd"],
    ):
        result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")


def _ensure_label(repo: str, label: str) -> None:
    try:
        _gh(["label", "create", label, "--repo", repo, "--color", "5319e7",
             "--description", "Picked up by the automated dispatcher", "--force"])
    except RuntimeError as exc:
        log.warning("could not ensure label %r exists: %s", label, exc)


def _fetch_candidate_issues(repo: str, state: str, exclude_labels: set[str], limit: int) -> list[dict]:
    out = _gh([
        "issue", "list",
        "--repo", repo,
        "--state", state,
        "--json", "number,title,body,labels,milestone",
        "--limit", str(limit),
    ])
    issues = json.loads(out)
    candidates = [
        issue for issue in issues
        if not exclude_labels & {l["name"] for l in issue.get("labels", [])}
    ]
    candidates.sort(key=_phase_sort_key)
    return candidates


def _mark(repo: str, number: int, label: str) -> None:
    _gh(["issue", "edit", str(number), "--repo", repo, "--add-label", label])


def _comment(repo: str, number: int, body: str) -> None:
    _gh(["issue", "comment", str(number), "--repo", repo, "--body", body])


def _truncate(text: str, limit: int = 300) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _check_scope(gh_issue: dict, max_turns: int) -> tuple[bool, str]:
    """Cheap pre-check: would this issue realistically fit in one PR within
    max_turns? Returns (too_large, reason)."""
    prompt = (
        f"Title: {gh_issue['title']}\n\n"
        f"Body:\n{gh_issue.get('body') or '(no description)'}\n\n"
        "An autonomous coding agent will attempt this issue in a single run, capped "
        f"at {max_turns} turns, and must end with one reviewable pull request. Judge "
        "whether this issue is scoped tightly enough to land as one coherent PR in "
        "that budget, or whether it actually bundles multiple independent features, "
        "pages, or changes that should be split into separate issues."
    )
    response = _client.messages.create(
        model="claude-opus-4-8",
        max_tokens=512,
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": _SCOPE_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)
    return result["too_large"], result["reason"]


def run(dry_run: bool = False) -> int:
    repo = os.environ["DISPATCH_REPO"]
    repo_path = os.environ["DISPATCH_REPO_PATH"]
    label = os.environ.get("DISPATCH_LABEL", "agent-dispatched")
    fail_label = os.environ.get("DISPATCH_FAIL_LABEL", "agent-incomplete")
    split_label = os.environ.get("DISPATCH_SPLIT_LABEL", "needs-splitting")
    scope_check = os.environ.get("DISPATCH_SCOPE_CHECK", "true").lower() != "false"
    state = os.environ.get("DISPATCH_STATE", "open")
    max_issues = int(os.environ.get("DISPATCH_MAX_ISSUES", "3"))
    base_branch = os.environ.get("DISPATCH_BASE_BRANCH", "main")
    max_turns = int(os.environ.get("DISPATCH_MAX_TURNS", "60"))
    max_budget_usd = float(os.environ.get("DISPATCH_MAX_BUDGET_USD", "5.0"))
    stack = os.environ.get("DISPATCH_STACK", "")
    extra_instructions = os.environ.get("DISPATCH_INSTRUCTIONS", "")
    if stack:
        stack_line = f"This repo is a {stack} project — follow its existing conventions and file structure."
        extra_instructions = f"{stack_line}\n\n{extra_instructions}" if extra_instructions else stack_line

    if dry_run:
        log.info("[dry-run] would ensure labels %r/%r/%r exist on %s (skipped)",
                  label, fail_label, split_label, repo)
    else:
        _ensure_label(repo, label)
        _ensure_label(repo, fail_label)
        _ensure_label(repo, split_label)

    try:
        candidates = _fetch_candidate_issues(repo, state, {label, fail_label, split_label}, limit=50)
    except RuntimeError as exc:
        log.error("failed to list issues for %s: %s", repo, exc)
        return 1

    candidates = candidates[:max_issues]
    if not candidates:
        log.info("no undispatched issues found for %s", repo)
        return 0

    log.info("%sdispatching %d issue(s) for %s: %s",
              "[dry-run] would start " if dry_run else "",
              len(candidates), repo, [c["number"] for c in candidates])

    failures = 0
    for gh_issue in candidates:
        number = gh_issue["number"]
        milestone_title = (gh_issue.get("milestone") or {}).get("title", "(none)")
        issue = {
            "number": number,
            "title": f"#{number} {gh_issue['title']}",
            "description": gh_issue.get("body") or "",
            "repo_path": repo_path,
        }

        if dry_run:
            prompt = _build_prompt(issue, extra_instructions)
            log.info("---- issue #%d [%s]: %s ----\n%s\n----",
                      number, milestone_title, gh_issue["title"], prompt)
            continue

        if scope_check:
            try:
                too_large, reason = _check_scope(gh_issue, max_turns)
            except Exception:
                log.exception("  issue #%d scope check failed, dispatching anyway", number)
                too_large, reason = False, ""
            if too_large:
                _mark(repo, number, split_label)
                _comment(repo, number, (
                    f"Skipped automated dispatch — this looks too large for a single PR: {reason}\n\n"
                    f"Labeled `{split_label}` and skipped on future runs. Consider splitting into "
                    "smaller issues."
                ))
                log.info("↷ issue #%d too large, labeled %r: %s", number, split_label, reason)
                continue

        try:
            _reset_repo(repo_path, base_branch)
        except RuntimeError as exc:
            failures += 1
            log.error("✗ issue #%d: failed to reset repo to a clean %s, skipping: %s",
                       number, base_branch, exc)
            continue

        log.info("→ starting issue #%d [%s]: %s", number, milestone_title, gh_issue["title"])
        error_message = None
        try:
            for event in dispatch(issue, extra_instructions, max_turns=max_turns, max_budget_usd=max_budget_usd):
                payload = json.loads(event[len("data: "):])
                if payload["type"] == "error":
                    error_message = payload["message"]
                    log.error("  issue #%d agent error: %s", number, error_message)
                elif payload["type"] == "result":
                    log.info("  issue #%d finished: %s", number, payload.get("stop_reason"))
                elif payload["type"] == "tool_use":
                    log.info("  issue #%d → %s(%s)", number, payload["name"], _truncate(json.dumps(payload["input"])))
                elif payload["type"] == "text":
                    log.info("  issue #%d: %s", number, _truncate(payload["text"]))

            if error_message:
                failures += 1
                _mark(repo, number, fail_label)
                _comment(repo, number, (
                    f"Automated dispatch did not complete: {error_message}\n\n"
                    f"Labeled `{fail_label}` and skipped on future runs — likely needs to be "
                    "split into smaller issues, or picked up manually."
                ))
                log.warning("✗ issue #%d incomplete, labeled %r", number, fail_label)
            else:
                _mark(repo, number, label)
                log.info("✓ issue #%d dispatched and labeled", number)
        except Exception:
            failures += 1
            log.exception("✗ issue #%d failed", number)
        finally:
            try:
                _reset_repo(repo_path, base_branch)
            except RuntimeError as exc:
                log.error("failed to reset repo to a clean %s after issue #%d: %s",
                           base_branch, number, exc)

    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and print what would be dispatched (prompt included) without "
             "running the agent, creating labels, or writing anything to GitHub.",
    )
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run))
