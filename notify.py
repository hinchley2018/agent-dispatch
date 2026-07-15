"""Post agent status updates to a Discord channel via incoming webhook."""

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)

USER_AGENT = "saywork-platform (https://github.com/code-nurturers/saywork-platform, 1.0)"


def post_discord(content: str) -> None:
    """Post *content* to the configured Discord webhook. Raises on failure or if unset."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")
    body = json.dumps({"content": content[:2000]}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    urllib.request.urlopen(req, timeout=5)


def notify_discord(content: str) -> None:
    """Best-effort version of post_discord(): failures are logged, not raised — used
    from agent-run code paths where a Discord hiccup must never break a task."""
    try:
        post_discord(content)
    except Exception:
        log.exception("Discord notification failed")
