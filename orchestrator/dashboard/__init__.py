"""Ops dashboard: a read-mostly FastAPI view over orchestrator state.

The only mutations it performs are the human directives from slice B
(un-quarantine an off_rails issue, resume a paused goal); everything else is a
read. Canonical state stays in Postgres and is reached only through
repository.py, so the event log the engine depends on is never bypassed.
"""
