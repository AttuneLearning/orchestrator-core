# Trying the Orchestrator on a Real LMS Project

A practical guide to running the orchestrator against one of your existing LMS
codebases. Read §0 first — it sets honest expectations about what each mode
actually does, so you point the right tool at the right job.

---

## 0. Two modes, and what each really does

There are two integration paths. They are complementary; most people start with
**Mode A** and graduate to **Mode B** on a throwaway branch.

| | **Mode A — Observe & coordinate** | **Mode B — Apply & verify** |
| --- | --- | --- |
| What it does | Decomposes a goal into per-team issues, runs them through the 5-phase pipeline, has teams message/triage each other, surfaces everything on the dashboard. Code from the model is **stored as advisory text**, never touched to your repo. | Same, but at the qa_gate the stored artifact is applied in a **disposable git worktree** of your repo and your real test/typecheck command runs against it. Promotion to a branch is **human-gated**. |
| Touches your repo? | **No.** Fully safe to run against any project. | Only a temporary worktree under `/tmp`; your working tree and branches are untouched until you run `apply-promote`, which does a **local** merge (never pushes). |
| Maturity | Solid — this is the core, fully tested. | Real but young. The artifact is whatever the code model returns as one blob; treat output as a draft, not a PR. |
| Start here if | You want to see how the system plans and coordinates work on your LMS, and inspect it. | You have a throwaway branch and a fast verify command and want to experiment with the closed loop. |

There is also a **third, separate thing** — the `agent-workflow/` submodule — which
installs the file-based `dev_communication/` protocol + skills into a repo for
**human/Claude-Code sessions** (not the autonomous engine). That's the most
battle-tested way to bring this methodology to a live LMS repo today; see §6.

> **Bottom line:** the orchestrator does not yet autonomously write correct,
> multi-file code into your LMS. It plans, coordinates, observes, and (in Mode B)
> drafts + verifies in a sandbox. Use it as a planning/coordination harness and
> an experiment platform, not a hands-off contributor.

---

## 1. Prerequisites

- Python 3.11+ and the orchestrator repo with all slices applied (you have this).
- Postgres 16/17 reachable (your podman container is fine). For semantic memory,
  the `pgvector/pgvector:pg17` image upgrades search from keyword to vector; plain
  Postgres works too (auto-degrades).
- For real output: an Anthropic API key (reasoning) and, optionally, your Qwen box
  for code generation — reachable from your machine on the LAN
  (`10.100.90.132:8081`), unlike from the cloud.
- A **copy or branch** of an LMS project you don't mind experimenting against.

Sanity-check the install:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m orchestrator.cli migrate          # applies 0001–0006
.venv/bin/python -m pytest -q                          # expect 92 passed, 1 skipped
```

---

## 2. Connect real models

Edit `.env` (copy from `.env.example` if needed). Defaults run hermetically on
stubs; these turn on real reasoning and code generation.

```bash
# Postgres (your podman container)
DATABASE_URL=postgresql://orchestrator:orchestrator@localhost:5432/orchestrator

# Reasoning agent — decompose, plan, gate-review, drift, triage, architect
ANTHROPIC_API_KEY=sk-ant-...
REASONING_MODEL=claude-opus-4-8

# Code agent — your Qwen box (LAN-reachable from your machine)
CODE_PROVIDER=openai
CODE_BASE_URL=http://10.100.90.132:8081/v1
CODE_MODEL=<your-qwen-model-name>
CODE_API_KEY=not-needed          # set if your endpoint checks it

# Optional: semantic memory (needs the pgvector image; else ignored)
EMBED_PROVIDER=stub              # or "openai" with EMBED_* set; "none" disables
```

Quick reachability check before you rely on it:

```bash
curl -s http://10.100.90.132:8081/v1/models | head -c 300   # should return JSON
```

If you'd rather keep everything on Anthropic for a first pass, set
`CODE_PROVIDER=anthropic` and skip the Qwen vars.

---

## 3. Mode A — observe & coordinate (safe, start here)

This never touches your LMS repo. You're watching the system *think about* the work.

```bash
# 1. Register a small team. Available teams: backend, frontend, qa, platform
#    (aliases: api, ui, quality, plat). Each needs a dev and a qa agent to flow.
.venv/bin/python -m orchestrator.cli register-agent --team backend --function dev
.venv/bin/python -m orchestrator.cli register-agent --team backend --function qa
.venv/bin/python -m orchestrator.cli register-agent --team frontend --function dev
.venv/bin/python -m orchestrator.cli register-agent --team frontend --function qa

# 2. Feed a goal phrased the way you'd brief a team lead about your LMS.
.venv/bin/python -m orchestrator.cli add-goal \
  "Add a course-completion certificate: PDF generated on 100% module completion, \
downloadable from the learner dashboard, with an API endpoint to fetch it." \
  --description "Existing stack: <name your framework/db>. Learners, Courses, \
Modules, Enrollment models already exist."

# 3. Watch it work. Daemon mode ticks continuously; or use --max-ticks for a burst.
.venv/bin/python -m orchestrator.cli serve-dashboard --port 8000 &   # http://127.0.0.1:8000
.venv/bin/python -m orchestrator.cli run --daemon --interval 5
```

On the dashboard you'll see the goal decompose into issues, route across
backend/frontend/qa, split into sub-issues if the architect judges them large,
and (with a real reasoner) get genuine gate reviews. Click any issue for its full
event timeline including the model's drafted code and review reasons.

**Try the cross-team loop:** drop a message in one team's inbox and watch the other
triage it into a local issue and answer when done:

```python
.venv/bin/python -c "
from orchestrator.config import load_settings
from orchestrator import db, repository as repo
s=load_settings(); pool=db.get_pool(s)
repo.create_message(pool,'frontend','api','Need certificate fetch endpoint',
  'GET /api/certificates/{enrollment_id} returning a signed PDF URL', priority='high')
print('queued')"
```

The next tick ingests it as a backend issue; on completion a response goes back to
frontend and the original is archived. Inspect with `cli status`.

If something drifts or quarantines, the dashboard banner shows it; clear it with
the Resume button (or `cli directive <id> resume --note "..."`) and restart a
paused goal with `cli goal-resume <id>`.

---

## 4. Mode B — apply & verify against your repo (experimental, sandboxed)

This is the closed loop: the qa_gate applies the drafted artifact in a **disposable
worktree** of your LMS repo and runs your real verify command there. Your working
tree, branches, and remotes are never touched until you explicitly promote.

### 4a. Pick a verify command for your stack

`VERIFY_CMD` runs inside the worktree; exit 0 = pass. Make it fast (lint/typecheck/
a focused test subset), not your whole CI suite. Examples:

| Stack | A reasonable `VERIFY_CMD` |
| --- | --- |
| Node / TypeScript | `npm ci --silent && npx tsc --noEmit` |
| Node / Jest subset | `npm ci --silent && npx jest --findRelatedTests --passWithNoTests` |
| Python / Django | `pip install -q -r requirements.txt && python -m pytest -q -x` |
| Python / ruff+mypy | `ruff check . && mypy .` |
| Rails | `bundle install --quiet && bin/rails test` |

### 4b. Configure and run on a throwaway branch

```bash
# In your LMS repo, make a branch you don't mind the worktree basing off:
git -C /path/to/lms checkout -b orchestrator-experiment

# In the orchestrator .env:
APPLY_ENABLED=true
APPLY_REPO_PATH=/path/to/lms
VERIFY_CMD=npx tsc --noEmit          # your choice from 4a
```

Then run a goal as in §3. At each qa_gate you'll get a `verification` event in the
issue timeline (pass/fail, returncode, captured stdout/stderr, the worktree branch
name `issue-<id>`). Worktrees live under `/tmp/orchestrator-worktrees/<repo-hash>/`.

### 4c. Promote — only you, only when it's good

Nothing merges automatically. When you've reviewed an issue's artifact and its
verification passed:

```bash
.venv/bin/python -m orchestrator.cli apply-promote <issue-id> --note "reviewed: looks right"
```

This does a **local** `--no-ff` merge of `issue-<id>` into your branch's current
HEAD. It refuses if the latest verification didn't pass. It never pushes — you
review the merge and push yourself if you want it.

> **Reality check:** the artifact is a single drafted blob written to
> `generated/issue-<id>.txt` in the worktree, not surgically edited into your
> source files. Mode B is best understood as "draft + sandboxed check + gated
> capture," a stepping stone — not a replacement for a developer applying the
> change. Read every artifact before promoting.

---

## 5. Safety & teardown

- **Nothing leaves your machine** except model API calls (Anthropic, and your LAN
  Qwen box). No pushes, ever — promotion is a local merge.
- **Wipe orchestrator state** (start clean) without touching your repo:
  ```bash
  .venv/bin/python -c "from orchestrator.config import load_settings; from orchestrator import db; \
  p=db.get_pool(load_settings()); \
  p.connection().__enter__().execute('TRUNCATE goals,issues,issue_events,agents,memory_notes,messages,adrs RESTART IDENTITY CASCADE')"
  ```
- **Remove worktrees** the apply leg created:
  ```bash
  git -C /path/to/lms worktree prune && rm -rf /tmp/orchestrator-worktrees
  ```
- **Kill switch:** set `APPLY_ENABLED=false` and the system is back to observe-only
  instantly — no code path touches your repo.

---

## 6. The complementary path: install the workflow into the LMS repo

Independent of the autonomous engine, the `agent-workflow/` submodule installs the
file-based `dev_communication/` protocol, the six skills (`/comms /adr /memory
/context /reflect /refine`), and ADR/pattern/session scaffolding **into a real
repo**, for use by human + Claude Code / Codex sessions. For a live LMS codebase
this is the most proven way to adopt the methodology today — it's how the protocol
was designed to be used.

```bash
# From the agent-workflow checkout (public: github.com/AttuneLearning/agent-workflow)
cd /path/to/agent-workflow
./agent-coord-setup.sh --both --detect-team        # or --claude-only / --codex-only
# Run it inside (or pointed at) your LMS repo; it scaffolds dev_communication/,
# registers the skills, and seeds the team registry. See its README for flags.
```

Then in a Claude Code session in that repo, `/context` to load relevant ADRs and
memory, `/comms` to triage inbound work, and the 5-phase lifecycle from
`PROCESS_GUIDE.md` applies. The orchestrator (Modes A/B) is the autonomous,
Postgres-backed evolution of exactly this protocol — you can run both: the file
workflow for human-driven work, the orchestrator to watch/coordinate the fleet.

---

## 7. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `add-goal` exits 1 "unknown pipeline" | Use `pipeline-1` (default), `hotfix`, or `research`. |
| Issues sit in `ready`, never advance | No idle agent for that team/function — register a dev **and** qa agent for the team the reasoner routed to (`cli status` shows teams). |
| Goal `paused`, dashboard banner | An issue failed or quarantined. Inspect its timeline, then `directive <id> resume` + `goal-resume <id>`. |
| Qwen calls fail / hang | Confirm `curl $CODE_BASE_URL/models` works from your machine; the box is LAN-only. Fall back to `CODE_PROVIDER=anthropic`. |
| pgvector test skipped | Expected on plain Postgres; run the `pgvector/pgvector:pg17` image to enable vector search. |
| Verify always fails in Mode B | Run your `VERIFY_CMD` by hand inside `/tmp/orchestrator-worktrees/<hash>/issue-<id>/` to see why; it's a normal checkout. |
| Apply leg did nothing | `APPLY_ENABLED` must be `true` **and** `APPLY_REPO_PATH` set **and** the issue must have reached qa_gate with a stored artifact. |
