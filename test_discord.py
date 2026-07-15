"""Manual check that DISCORD_WEBHOOK_URL is set and the webhook actually works.

Usage: venv/bin/python test_discord.py
"""

import sys
import urllib.error

from dotenv import load_dotenv

load_dotenv()

from notify import post_discord  # noqa: E402  (must import after load_dotenv)


def main() -> int:
    print("Posting test message to Discord...")
    try:
        post_discord("🧪 Test notification from saywork-platform — Discord webhook is working.")
    except urllib.error.HTTPError as exc:
        print(f"Discord rejected the request: HTTP {exc.code} — {exc.read().decode(errors='replace')}")
        return 1
    except urllib.error.URLError as exc:
        print(f"Could not reach Discord: {exc.reason}")
        return 1
    except RuntimeError as exc:
        print(str(exc))
        return 1

    print("Sent. Check the Discord channel to confirm it arrived.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
