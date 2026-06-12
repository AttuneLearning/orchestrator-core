"""CLI session worker: the code-generation leg for runtime=cli agents.

Wraps a long-lived local coding session (e.g. `claude -p ... --resume <id>`)
behind the same implement() contract as ApiWorker. The command template comes
from settings.cli_agent_cmd with {prompt} and {session_id} placeholders; the
subprocess's stdout is the code artifact and is STORED, NEVER EXECUTED — the
session itself is the sandboxed worker.

The per-issue session id is stable (issue-{id}), persisted as a session_started
event the first time, so a re-engaged or restarted engine resumes the same
session rather than starting fresh.
"""

from __future__ import annotations

import shlex
import subprocess

from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..config import Settings
from ..models import Issue
from .base import CodeResult

_TIMEOUT_S = 300


class CliSessionWorker:
    def __init__(self, settings: Settings):
        self._cmd_template = settings.cli_agent_cmd

    def implement(self, pool: ConnectionPool, issue: Issue) -> CodeResult:
        if not self._cmd_template:
            raise RuntimeError(
                "runtime=cli agent assigned but CLI_AGENT_CMD is not configured"
            )
        session_id = f"issue-{issue.id}"
        if not any(e.event_type == "session_started"
                   for e in repo.recent_events(pool, issue.id, limit=200)):
            repo.append_log(pool, issue.id, "session_started",
                            {"session_id": session_id})

        prompt = f"Implement issue '{issue.title}'. {issue.description}"
        argv = [
            part.format(prompt=prompt, session_id=session_id)
            for part in shlex.split(self._cmd_template)
        ]
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"cli session exited {proc.returncode}: {proc.stderr[:500]}"
            )
        content = proc.stdout.strip()
        result = CodeResult(content=content, provider="cli", model=argv[0])
        repo.append_log(
            pool, issue.id, "code_generated",
            {"provider": "cli", "session_id": session_id, "model": argv[0],
             "chars": len(content), "content": content},
        )
        return result
