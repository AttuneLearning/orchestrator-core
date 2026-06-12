"""API worker: the code-generation leg for the implementation gate.

Calls the configured code client and persists the output to the issue_events
log via the repository. Generated code is STORED, NEVER EXECUTED.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from .. import repository as repo
from ..config import Settings
from ..models import Issue
from .base import CodeResult
from .providers import CodeClient, make_code_client


class ApiWorker:
    def __init__(self, settings: Settings, client: CodeClient | None = None):
        self._client = client or make_code_client(settings)

    def implement(self, pool: ConnectionPool, issue: Issue) -> CodeResult:
        prompt = f"Implement issue '{issue.title}'.\n\n{issue.description}"
        result = self._client.generate(prompt)
        repo.append_log(
            pool,
            issue.id,
            "code_generated",
            {
                "provider": result.provider,
                "model": result.model,
                "chars": len(result.content),
                # store the artifact itself; it is never executed
                "content": result.content,
            },
        )
        return result
