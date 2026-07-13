# Open Source Model Startup Plan

> 📋 **Planning/design doc, partially implemented.** The bootstrap flow it
> envisions now largely exists as the `setup-project` / `install-launchers` CLI
> commands. For runnable adoption steps use [`INSTALL.md`](INSTALL.md); read this
> for the design rationale behind the launcher kit and multi-runtime model plumbing.

This document defines the launcher work needed to make open-source or open-source-compatible models usable for simple orchestrator tasks without the current session stalling after a few tool calls.

Scope:

- `python-orchestrator-v1` launcher templates and model profile plumbing.
- The live workspace at `~/github/tendcharting-ws`, which has its own parallel startup scripts and Qwen worker path.
- Startup targets for `codex`, `opencode`, and `qwen code`.
- A project bootstrap direction for setting up workspace roots, worktrees, and starter scripts from a central orchestrator source of truth.

This is a planning document only. It does not change code.

---

## Why this is needed

The current setup is split across multiple launcher stacks and multiple model-selection mechanisms.

Observed problems:

- Codex-backed sessions can start, make a few tool calls, and then stop responding.
- The same failure pattern shows up when different open-source or OpenAI-compatible models are assigned, including GLM and DeepSeek v4 Pro.
- The current launchers mix role resolution, model profile resolution, MCP wiring, and loop management in a way that is hard to reason about and hard to debug.
- `~/github/tendcharting-ws` has another launcher stack of its own, including a Qwen worker runtime and a separate poll loop, so there is no single source of truth for startup behavior.

The result is a system that is partially wired, but not yet trustworthy for routine use.

---

## Project Bootstrap Direction

There needs to be a project-level `setup project` script or equivalent setup direction before the runtime launchers can be considered complete.

### What the setup step should do

- Create the parent workspace directory for the project, named `<project>-ws`.
- Create one worktree repository per planned subagent role, using the roles already defined in the orchestrator YAML for that project.
- Create a dedicated `humantest-wt` worktree that acts as the local CI/CD consolidation point.
- Ensure completed work is merged or consolidated into `humantest-wt` before human testing begins.
- Copy the base launcher scripts from the central orchestrator location into the project workspace.
- Register the supported CLI runtimes from those copied scripts:
  - Claude Code
  - Codex from OpenAI
  - Codex against an open-source-compatible backend
  - Qwen Code
  - OpenCode
  - any other runtime that the orchestrator manifest explicitly supports later
- Wire those launchers back to the single source of truth config files created by the default templates and then edited through the orchestrator settings page.

### Initial scope only

At the start, the setup direction should stop after scaffolding and secure wiring. It should not:

- start agents automatically,
- apply product code changes,
- invent project-specific runtime behavior,
- or duplicate configuration logic in each worktree.

The point of the first setup step is to make the project bootstrappable, editable, and safe to extend.

### Secret and editability requirements

- API keys must live in secure locations, not in tracked launcher scripts.
- The setup step should reference secret locations by environment variable name or secure external path, depending on the runtime.
- The orchestrator project directory and the project workspace directories should be enabled for the correct level of editing needed to support launcher setup and configuration updates.
- Editing access should be narrow by default, with no broader permissions than the launcher and configuration workflow requires.

---

## Orch-Manager Guided Setup Flow

The first `orch-manager` session should be able to walk a new user through project setup instead of assuming the workspace already exists.

### What the first orch-manager should do

- Ask for the project name.
- Detect whether the user wants to:
  - use the existing workflow unchanged,
  - or customize the workflow for this project.
- If the user chooses customization, ask only the minimum necessary questions to determine:
  - workspace layout,
  - role names,
  - runtime preferences,
  - model profile source,
  - and whether any project-specific wrappers are needed.
- Translate the answers into a project bootstrap plan that fits the `setup project` direction above.
- Keep the session focused on setup, not on implementation details that can be deferred.

### Questions the orch-manager should ask

- What is the project name?
- Should this project use the existing workflow as-is, or customize it?
- Which runtimes should be enabled initially?
- Should the project use the default role layout from the orchestrator YAML, or a custom role map?
- Where should secrets live for this project?
- Should the project keep the default `humantest-wt` consolidation flow, or customize the handoff point?

### Base instructions the orch-manager should provide

- Create the `<project>-ws` parent workspace first.
- Create the child worktrees next, one per planned role.
- Create `humantest-wt` as the main consolidation and human-testing repository.
- Copy the canonical startup scripts into the project workspace from the orchestrator source of truth.
- Wire the launchers to the shared configuration generated by templates and edited through the orchestrator settings page.
- Place API keys in secure locations and reference them by name, not by value.
- Do not expand scope beyond the bootstrap and configuration steps until the workspace is verified.

### Child worker instantiation guidance

The orch-manager should also provide base instructions for creating the child workers so a new user can see the whole operating model:

- Each planned role becomes one child worker worktree.
- Each child worker starts from the canonical launcher pattern for its runtime.
- The worker identity should be explicit:
  - project,
  - role,
  - team,
  - function,
  - runtime,
  - and worktree.
- The child worker should inherit model profile selection from the shared config, not from ad hoc local edits.
- The first run of each child worker should be a dry run or validation run before active task execution.

### Issue reasoner guidance

The orch-manager should explain the issue "reasoner" as the policy layer that decides how work moves through the project.

- The reasoner turns project work into roles, worktrees, and task routing.
- The reasoner decides whether a task belongs in:
  - the orch-manager,
  - a child worker,
  - or the human-test consolidation path.
- The reasoner should remain separate from the launcher code.
- The reasoner should be able to read the orchestrator YAML and project settings so it can keep routing consistent with the chosen workflow.

---

## Current State

Current state means "what exists now, but is not functional enough to rely on".

### What exists already

- `templates/project-launchers/start-agent.sh` and `templates/project-launchers/start-orch-manager.sh` provide a role-based launcher surface.
- `templates/project-launchers/agent-launchers/runtimes/codex.sh` already knows about multiple inference profiles.
- `templates/project-launchers/agent-launchers/runtimes/qwen.sh` already has a Qwen-specific path.
- `templates/project-launchers/agent-launchers/model-settings.py` already tries to read dashboard-managed model settings.
- The TendCharting workspace has its own launcher stack:
  - `~/github/tendcharting-ws/start-agent.sh`
  - `~/github/tendcharting-ws/start-dev-worker.sh`
  - `~/github/tendcharting-ws/start-qa-worker.sh`
  - `~/github/tendcharting-ws/start-qwen-code-worker.sh`
  - `~/github/tendcharting-ws/run-agent-loop.sh`
  - `~/github/tendcharting-ws/tendcharting-root-old/qwen-agent/` for the archived legacy worker implementation

### Why it is not functional

- There is no single canonical launcher contract for "simple tasks on open-source models".
- The current Codex path is still sensitive to wire protocol details and model-provider shape.
- Qwen Code is effectively treated as a separate worker system instead of a first-class runtime alongside Codex and OpenCode.
- The repeated startup layers make it hard to tell whether failure is happening in:
  - model selection,
  - MCP registration,
  - worker loop restart logic,
  - tool-call compatibility,
  - or the agent runtime itself.
- In `tendcharting-ws`, the live launcher tree and the backup/script archive tree are both present, which adds noise and makes it harder to see what is authoritative.

### Current-state goal

The current state should be treated as a diagnosis state only. It is useful for understanding the shape of the problem, but not for dependable agent work.

---

## Moderate State

Moderate state means "the launchers work consistently enough that the orchestrator can use open-source models for simple tasks without special handling".

### Properties of the moderate state

- One canonical startup path per runtime.
- A shared model profile resolver for all runtimes.
- A consistent way to inject:
  - model name,
  - base URL,
  - API key environment variable,
  - wire API mode,
  - and reasoning effort or equivalent runtime hints.
- A consistent MCP bootstrap for all launchers.
- A clear distinction between:
  - orchestration-only roles,
  - pull-worker roles,
  - and on-demand manager or escalation roles.
- A repeatable loop policy:
  - one turn,
  - or one issue cycle,
  - plus an explicit wrapper for polling or re-entry when the runtime does not stay alive on its own.

### Proposed launcher shape

- `start-orch-manager.sh`
  - starts a manager session from the workspace root.
- `start-dev-worker.sh`
  - starts a developer worker for a team and runtime.
- `start-qa-worker.sh`
  - starts a QA worker for a team and runtime.
- `start-codex.sh`
  - Codex adapter, parameterized by model profile.
- `start-opencode.sh`
  - OpenCode adapter, parameterized by model profile.
- `start-qwen-code.sh`
  - Qwen Code adapter, parameterized by model profile or Qwen-specific config.

### What the moderate state should guarantee

- Dry-run output shows the resolved runtime, model, base URL, and MCP target before anything launches.
- A launcher can be pointed at a local or remote OpenAI-compatible endpoint and still run more than a few tool calls.
- The same issue can be run under Codex, OpenCode, or Qwen Code without changing the orchestrator contract.
- If one runtime cannot sustain a session, the wrapper explicitly restarts it rather than silently dying.

### Minimum validation for moderate state

- `--dry-run` is implemented for every launcher.
- MCP registration is verified before the agent starts.
- A smoke test confirms:
  - the runtime can load the prompt,
  - the MCP server responds,
  - and the agent survives multiple sequential tool calls.
- Open-source endpoints are validated against the same launcher contract, not by ad hoc manual tweaks.

---

## Future State

Future state means "the launcher system is boring, deterministic, and easy to extend".

### Properties of the future state

- Startup scripts are generated from one declarative manifest instead of being hand-maintained in multiple places.
- The manifest can describe:
  - role,
  - runtime,
  - model profile,
  - MCP target,
  - loop policy,
  - and workspace-specific overrides.
- Codex, OpenCode, and Qwen Code all use the same orchestration contract.
- Open-source model endpoints can be swapped without editing runtime scripts.
- A runtime failure is visible as:
  - a launch-time validation error,
  - a loop-level restart,
  - or a clear health check failure.
  It should not present as "the agent just went quiet."
- The launcher tree is small enough that new contributors can tell which file is authoritative without reading backup directories or old session notes.

### Future-state acceptance criteria

- A fresh session can be launched with a single command per role.
- The selected model profile is explicit and visible in dry-run output.
- The agent can complete a simple task that requires multiple tool calls without falling out of the loop.
- The same startup path works for both local open-source endpoints and remote OpenAI-compatible gateways.
- The repo contains a clear migration path for any older launcher scripts.

---

## Recommended Implementation Phases

Each phase below should end with a light verification step. The tests are intentionally small: they should prove the phase is complete without requiring a full production rollout.

### Phase 1: Inventory and canonicalize

- Inventory every launcher entrypoint in the orchestrator repo and `~/github/tendcharting-ws`.
- Decide which files are canonical and which are compatibility wrappers.
- Define one naming convention for the three runtimes:
  - `codex`
  - `opencode`
  - `qwen-code`
- Define one shared model profile schema.

Light tests:

- `python -m pytest tests/test_model_profiles.py`
- `python -m orchestrator.cli install-launchers --dry-run ...` shows the same canonical launcher tree for every workspace.
- A dry-run inventory confirms only one file path is treated as authoritative for each launcher role.

### Phase 2: Normalize startup contract

- Make every launcher accept the same base flags.
- Make `--dry-run` print:
  - role,
  - runtime,
  - model profile,
  - model name,
  - base URL,
  - API key source,
  - and MCP target.
- Keep prompt loading and MCP wiring shared.

Light tests:

- `--dry-run` for `start-agent.sh`, `start-orch-manager.sh`, and the runtime adapters prints the same canonical identity fields.
- A launcher dry run resolves a profile-backed model name, base URL, and API key source without touching the runtime.
- The launch summary shows the same role/worktree mapping for Codex, OpenCode, and Qwen Code.

### Phase 2b: Add project bootstrap direction

- Define the `setup project` script or command sequence that creates the workspace root, role worktrees, and `humantest-wt`.
- Make the setup step copy the canonical launcher base scripts from the orchestrator source of truth into the project workspace.
- Make the setup step register the supported runtimes against the single shared config source generated by templates and maintained through the orchestrator settings page.
- Add explicit instructions for secure API key placement and for the minimum editing permissions needed by the orchestrator and project directories.
- Keep the setup step narrow: bootstrap only, no execution or runtime policy expansion.

Light tests:

- `setup-project --dry-run` prints the parent workspace, planned child worktrees, `humantest-wt`, and launcher install plan.
- A setup dry run resolves the worktree list from the orchestrator roster or YAML source rather than from hard-coded names.
- The setup flow reports where secrets must live without emitting secret values.

Implementation progress:

- `orchestrator.cli setup-project` now exists as the first bootstrap slice.
- Launcher template copying now skips `__pycache__` and `.pyc` artifacts.
- Tests cover the roster-derived worktree plan and the workspace bootstrap write path.
- OpenCode startup support now has a launcher wrapper, an orch-manager wrapper, a runtime adapter, and bootstrap coverage in `tests/test_project_bootstrap.py`.
- The OpenCode runtime path is wired to the shared model settings source rather than hard-coded local config.
- The focused bootstrap test suite currently passes for the launcher and runtime slice that has been implemented.

### Phase 3: Separate runtime adapters from orchestration

- Keep role resolution in one place.
- Keep runtime-specific model wiring in one place.
- Keep worker-loop behavior in one place.
- Do not mix launcher policy with orchestrator state logic.

Light tests:

- Each runtime adapter can be updated independently while preserving the same launcher CLI.
- A role resolution test proves the same role maps to the same worktree path across supported launchers.

### Phase 4: Add smoke tests

- Add tests for:
  - dry-run resolution,
  - profile loading,
  - MCP registration,
  - and repeated tool-call survival.
- Add tests that cover:
  - a local open-source endpoint,
  - a remote OpenAI-compatible endpoint,
  - and a Qwen-specific path.

Light tests:

- A local or fixture-backed endpoint can complete multiple sequential tool calls without the launcher falling out of the loop.
- The smoke suite can run against dry-run mode only when the model backend is unavailable.

### Phase 5: Roll out by role

- Enable the simplest simple-task roles first.
- Verify the worker can complete a short task and return control cleanly.
- Only then expand to manager and escalation roles.

Light tests:

- One simple dev or QA role can be launched end-to-end from the new setup flow.
- A worker can complete a small issue and return control to the orch-manager without manual intervention.

---

## Repo Cleanup Recommendations

### In `python-orchestrator-v1`

- Resolve the current uncommitted model-profile and launcher edits into a single reviewed change set instead of leaving them split across local modifications.
- Keep `templates/project-launchers/` as the authoritative launcher source or replace it with another canonical location, but do not maintain two independent launcher trees.
- Remove any stale or redundant startup docs once the new plan is implemented.
- Keep runtime logs, local backups, and generated artifacts out of version control.
- If a launcher script is only a compatibility shim, label it clearly and keep it thin.

### In `~/github/tendcharting-ws`

- Choose one launcher source of truth:
  - either the top-level shell wrappers,
  - or a generated launcher layer,
  - but not both as competing hand-edited systems.
- Archive or delete obsolete backup trees such as `script-backups/` and other dated WIP backup directories once they are no longer needed.
- Treat `start-agent.sh`, `run-agent-loop.sh`, and the Qwen worker path as separate concerns:
  - role selection,
  - lifecycle control,
  - and model execution.
- Remove or ignore generated workspace noise such as:
  - `.claude/`,
  - `.qwen/`,
  - `deploy/.env.acceptance`,
  - and other local-only artifacts if they are not meant to be committed.
- Replace duplicated role documents with one generated startup source per runtime.
- Add a project bootstrap entrypoint or documented setup flow that creates the parent workspace, the agent worktrees, and the `humantest-wt` consolidation repo from orchestrator-managed configuration.
- Move API key handling out of launcher content and into secure runtime-specific secret locations referenced by name only.

### Cleanup outcome to aim for

- `git status` should be clean in the repo that owns the launcher code.
- The launcher tree should read as a small set of canonical entrypoints, not a pile of historical experiments.
- Anyone should be able to tell, from one file, how Codex, OpenCode, and Qwen Code are supposed to start.

---

## Suggested Definition of Done

- One canonical launcher contract exists for all three runtimes.
- Open-source model selection is profile-driven, not hard-coded.
- Codex sessions no longer die after a few tool calls for simple tasks.
- OpenCode and Qwen Code can be substituted without changing the orchestrator contract.
- The project can be bootstrapped from a `setup project` flow that creates the workspace layout, copies the canonical launchers, and references secure secrets without embedding them.
- The repo is cleaned up enough that a new launcher change can be reviewed without wading through backups and duplicate wrappers.

---

## Completion Checklist

Use this checklist as the oriented end state for the whole document.

- [ ] The project has a single documented `setup project` direction or script.
- [ ] The first orch-manager can ask for the project name.
- [ ] The first orch-manager can ask whether to use the existing workflow or customize it.
- [ ] The first orch-manager can translate that choice into a bootstrap path.
- [ ] The parent `<project>-ws` workspace is created first.
- [ ] All planned `"<agent>-wt"` child worktrees are created from the orchestrator YAML role map.
- [ ] A `humantest-wt` repository exists as the local consolidation and human-testing target.
- [ ] Completed work is routed into `humantest-wt` before human testing.
- [ ] The canonical launcher base scripts are copied from the central orchestrator source of truth.
- [ ] The supported runtimes are registered from the shared configuration layer.
- [ ] API keys are stored only in secure locations and referenced by name.
- [ ] The orchestrator and project directories have only the minimum required editing access.
- [ ] The orch-manager can explain how child workers are instantiated.
- [ ] The orch-manager can explain how the issue reasoner routes work.
- [ ] Codex, OpenCode, and Qwen Code share one startup contract.
- [ ] Open-source model selection is driven by profile, not hard-coded runtime edits.
- [ ] Dry-run validation exists before active execution.
- [ ] The repo has been cleaned of redundant launcher trees, backup clutter, and duplicate docs.
- [ ] The launcher system is understandable from the canonical docs without searching backups.
