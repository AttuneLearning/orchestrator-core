from orchestrator import cli


def test_contract_sync_detects_seed_contracts_and_api_routes():
    changed = {
        "contracts.seed.json",
        "packages/contracts/src/appointments.ts",
        "apps/api/src/app.ts",
        "apps/api/src/clinicians/cliniciansRouter.ts",
        "apps/web/src/shared/api/cliniciansApi.ts",
        "README.md",
    }

    assert cli._contract_relevant_changed_files(changed) == [
        "apps/api/src/app.ts",
        "apps/api/src/clinicians/cliniciansRouter.ts",
        "contracts.seed.json",
        "packages/contracts/src/appointments.ts",
    ]


def test_contract_sync_supports_custom_seed_path():
    changed = {
        "config/contracts.seed.json",
        "contracts.seed.json",
    }

    assert cli._contract_relevant_changed_files(
        changed, seed_rel="config/contracts.seed.json"
    ) == ["config/contracts.seed.json"]
