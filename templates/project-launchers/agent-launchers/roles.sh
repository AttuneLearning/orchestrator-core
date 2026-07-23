#!/usr/bin/env bash

# ---------------------------------------------------------------------------
# AGENT_SIDECAR (durable-worker-sidecar plan, Phase 5): set AGENT_SIDECAR=1
# in the environment before invoking start-agent.sh to route a role through
# the durable-worker side-car (agent-launchers/sidecar.py) instead of the
# per-cycle relaunch loop (run-agent-loop.sh). The side-car injects "tick"
# prompts into ONE long-lived worker session/pane forever, instead of
# re-execing the agent every poll interval. See start-agent.sh for the
# branch that reads this flag and plans/durable-worker-sidecar-plan.md for
# the full design.
#
#   - opencode roles: the side-car spawns/owns `opencode serve` itself
#     (HTTP adapter) on a per-agent port (4900+AGENT_ID).
#   - claude/codex roles: require SIDECAR_TMUX_TARGET (the operator's tmux
#     pane, e.g. "agents:1.0") in the environment -- the side-car drives that
#     pane via tmux capture-pane/send-keys instead of owning a subprocess.
#
# This file (roles.sh) makes NO default-flip: no role sets AGENT_SIDECAR
# here, and none should until the operator explicitly opts a role in via the
# environment at launch time. AGENT_SIDECAR unset/0 (the default for every
# role below) is byte-identical to pre-Phase-5 behavior.
# ---------------------------------------------------------------------------

resolve_role() {
  ROLE_INPUT="${1:?role required}"
  ROLE="$ROLE_INPUT"
  AGENT_ID=""
  TEAM=""
  FUNCTION=""
  GATE=""
  APP=""
  WORKTREE="$WORKSPACE_ROOT"
  PROMPT_NAME=""
  DEFAULT_RUNTIME=""
  LOOP_AGENT=0
  IDLE_STOP=0
  COMMAND_TIMEOUT=0

  case "$ROLE_INPUT" in
    orch|orch-manager|orchestrator|orchestrator-manager)
      ROLE="orch-manager"
      TEAM="orchestration"
      FUNCTION="lead"
      WORKTREE="$WORKSPACE_ROOT"
      PROMPT_NAME="orch-manager"
      DEFAULT_RUNTIME="claude"
      ;;
    backend-dev-manager|be-dev-manager)
      ROLE="backend-dev-manager"
      AGENT_ID=1
      TEAM="backend"
      FUNCTION="dev-manager"
      GATE="implementation"
      APP="apps/api"
      WORKTREE="$WORKSPACE_ROOT/wt-backend-dev"
      PROMPT_NAME="dev-manager"
      DEFAULT_RUNTIME="claude"
      LOOP_AGENT=1
      COMMAND_TIMEOUT=1200
      ;;
    frontend-dev-manager|fe-dev-manager)
      ROLE="frontend-dev-manager"
      AGENT_ID=3
      TEAM="frontend"
      FUNCTION="dev-manager"
      GATE="implementation"
      APP="apps/web"
      WORKTREE="$WORKSPACE_ROOT/wt-frontend-dev"
      PROMPT_NAME="dev-manager"
      DEFAULT_RUNTIME="claude"
      LOOP_AGENT=1
      COMMAND_TIMEOUT=1200
      ;;
    backend-dev-worker|backend-dev|be-dev)
      ROLE="backend-dev-worker"
      AGENT_ID=1
      TEAM="backend"
      FUNCTION="dev"
      GATE="implementation"
      APP="apps/api"
      WORKTREE="$WORKSPACE_ROOT/wt-backend-dev"
      PROMPT_NAME="dev-worker"
      DEFAULT_RUNTIME="opencode"
      # Route through run-agent-loop.sh; the dashboard (loop_enabled +
      # poll_interval_seconds) decides loop-vs-one-shot and cadence at runtime.
      LOOP_AGENT=1
      ;;
    backend-dev-worker-2|backend-dev-2|be-dev-2)
      # Second backend dev lane (agent 8) for parallelism — own agent_id + own
      # worktree so it does NOT collide with agent 1 (wt-backend-dev). Steer it to
      # work independent of agent 1 (e.g. different goal/files) to avoid promote conflicts.
      ROLE="backend-dev-worker-2"
      AGENT_ID=8
      TEAM="backend"
      FUNCTION="dev"
      GATE="implementation"
      APP="apps/api"
      WORKTREE="$WORKSPACE_ROOT/wt-backend-dev-2"
      PROMPT_NAME="dev-worker"
      DEFAULT_RUNTIME="opencode"
      # Route through run-agent-loop.sh; the dashboard (loop_enabled +
      # poll_interval_seconds) decides loop-vs-one-shot and cadence at runtime.
      LOOP_AGENT=1
      ;;
    frontend-dev-worker|frontend-dev|fe-dev)
      ROLE="frontend-dev-worker"
      AGENT_ID=3
      TEAM="frontend"
      FUNCTION="dev"
      GATE="implementation"
      APP="apps/web"
      WORKTREE="$WORKSPACE_ROOT/wt-frontend-dev"
      PROMPT_NAME="dev-worker"
      DEFAULT_RUNTIME="opencode"
      # Route through run-agent-loop.sh; the dashboard (loop_enabled +
      # poll_interval_seconds) decides loop-vs-one-shot and cadence at runtime.
      LOOP_AGENT=1
      ;;
    backend-qa-worker|backend-qa|be-qa)
      ROLE="backend-qa-worker"
      AGENT_ID=2
      TEAM="backend"
      FUNCTION="qa"
      GATE="verification"
      APP="apps/api"
      WORKTREE="$WORKSPACE_ROOT/wt-backend-qa"
      PROMPT_NAME="qa-worker"
      DEFAULT_RUNTIME="opencode"
      LOOP_AGENT=1
      ;;
    frontend-qa-worker|frontend-qa|fe-qa)
      ROLE="frontend-qa-worker"
      AGENT_ID=4
      TEAM="frontend"
      FUNCTION="qa"
      GATE="verification"
      APP="apps/web"
      WORKTREE="$WORKSPACE_ROOT/wt-frontend-qa"
      PROMPT_NAME="qa-worker"
      DEFAULT_RUNTIME="opencode"
      LOOP_AGENT=1
      ;;
    senior|senior-dev|sr-dev|escalation)
      ROLE="senior-dev"
      AGENT_ID=5
      TEAM="senior"
      FUNCTION="dev"
      GATE="implementation"
      APP="apps/api apps/web packages/contracts"
      WORKTREE="$WORKSPACE_ROOT/wt-senior-dev"
      PROMPT_NAME="senior-dev"
      DEFAULT_RUNTIME="claude"
      LOOP_AGENT=1
      IDLE_STOP=2
      ;;
    senior-qa|senior-qa-worker|sr-qa)
      # Cross-team QA: the 'senior' team is exempt from the claim team-guard, so
      # agent 6 can verify pre-assigned issues in any lane (apps/api + apps/web).
      ROLE="senior-qa-worker"
      AGENT_ID=6
      TEAM="senior"
      FUNCTION="qa"
      GATE="verification"
      APP="apps/api apps/web packages/contracts"
      WORKTREE="$WORKSPACE_ROOT/wt-senior-qa"
      PROMPT_NAME="qa-worker"
      DEFAULT_RUNTIME="opencode"
      LOOP_AGENT=1
      IDLE_STOP=2
      ;;
    *)
      echo "unknown role: $ROLE_INPUT" >&2
      echo "known roles: orch-manager, backend-dev-manager, frontend-dev-manager, backend-dev-worker, backend-dev-worker-2, frontend-dev-worker, backend-qa-worker, frontend-qa-worker, senior-dev, senior-qa" >&2
      return 1
      ;;
  esac
}
