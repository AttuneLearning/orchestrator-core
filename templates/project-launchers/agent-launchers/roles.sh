#!/usr/bin/env bash

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
      LOOP_AGENT=0
      ;;
    backend-dev-worker-2|backend-dev-2|be-dev-2)
      # OPTIONAL second backend dev lane for parallelism. Requires a registered
      # agent (id 8) + its own worktree (wt-backend-dev-2) so it does NOT collide
      # with agent 1. Launch: ./start-dev-worker.sh backend-2 <runtime>. Steer it to
      # work independent of agent 1 (different goal/files) to avoid promote conflicts.
      ROLE="backend-dev-worker-2"
      AGENT_ID=8
      TEAM="backend"
      FUNCTION="dev"
      GATE="implementation"
      APP="apps/api"
      WORKTREE="$WORKSPACE_ROOT/wt-backend-dev-2"
      PROMPT_NAME="dev-worker"
      DEFAULT_RUNTIME="opencode"
      LOOP_AGENT=0
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
      LOOP_AGENT=0
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
      echo "known roles: orch-manager, backend-dev-manager, frontend-dev-manager, backend-dev-worker, frontend-dev-worker, backend-qa-worker, frontend-qa-worker, senior-dev, senior-qa" >&2
      return 1
      ;;
  esac
}
