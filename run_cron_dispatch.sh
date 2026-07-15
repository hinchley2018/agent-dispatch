#!/usr/bin/env bash
# Wrapper for cron/systemd: cron runs with a bare environment, so this
# resolves paths relative to itself and lets cron_dispatch.py's load_dotenv()
# pick up backend/.env.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# Only needed if running as root: tells Claude Code this box is a dedicated,
# isolated sandbox so it's safe to allow bypassPermissions as root. Do not set
# this on a shared/production machine or one where issue creation isn't trusted.
export IS_SANDBOX=1
exec venv/bin/python cron_dispatch.py
