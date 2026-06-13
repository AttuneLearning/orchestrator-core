"""Pure tests for roster sub-team mode/runtime parsing (pull model)."""

from orchestrator.config import load_settings
from orchestrator.roster import load_roster


def test_subteam_mode_runtime_round_trip():
    cfg = {
        "default_runtime": "external",
        "active_teams": [
            {
                "id": "backend",
                "role": "backend",
                "repos": ["api-repo"],
                "sub_teams": [
                    {"id": "backend-dev", "function": "dev",
                     "mode": "pull", "runtime": "external"},
                    {"id": "backend-qa", "function": "qa",
                     "mode": "pull", "runtime": "external"},
                    {"id": "backend-lead", "function": "lead",
                     "mode": "verdict", "runtime": "api"},
                ],
            }
        ],
    }
    roster = load_roster(cfg)
    subs = {s.id: s for s in roster.resolve("backend").sub_teams}
    assert subs["backend-dev"].mode == "pull"
    assert subs["backend-dev"].runtime == "external"
    assert subs["backend-lead"].function == "lead"
    assert subs["backend-lead"].mode == "verdict"
    assert subs["backend-lead"].runtime == "api"


def test_subteam_runtime_defaults_to_config_default_runtime():
    cfg = {
        "default_runtime": "external",
        "active_teams": [
            {"id": "t", "sub_teams": [{"id": "t-dev", "function": "dev"}]},
        ],
    }
    sub = load_roster(cfg).resolve("t").sub_teams[0]
    # runtime falls back to default_runtime; mode defaults to verdict
    assert sub.runtime == "external"
    assert sub.mode == "verdict"


def test_default_config_subteams_unchanged():
    # The shipped roster (config/roster.yaml) still parses; defaults applied.
    roster = load_roster(load_settings().roster)
    backend = roster.resolve("backend")
    assert backend is not None
    for sub in backend.sub_teams:
        assert sub.mode in ("pull", "verdict")
        assert sub.runtime in ("api", "cli", "external")
