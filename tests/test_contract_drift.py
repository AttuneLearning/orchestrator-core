"""Pure-core tests for contract drift checker.

Tests the diff_contracts function and endpoint parsers with injected data.
No DB, no filesystem fixtures — purely unit-level testing of logic.
"""

from __future__ import annotations

import pytest

from orchestrator.contract_drift import (
    diff_contracts,
    parse_endpoint_registry,
    parse_msw_handlers,
)


# ============================================================================ #
# Test helpers
# ============================================================================ #


def findings_by_category(findings: list[dict]) -> dict[str, list[dict]]:
    """Group findings by category for easier assertions."""
    by_cat = {}
    for f in findings:
        cat = f["category"]
        if cat not in by_cat:
            by_cat[cat] = []
        by_cat[cat].append(f)
    return by_cat


# ============================================================================ #
# Tests: pure diff core (diff_contracts)
# ============================================================================ #


def test_clean_registry_no_findings() -> None:
    """One active contract with matching audit route and FE ref => no findings."""
    contracts = [
        {
            "id": 1,
            "method": "GET",
            "path": "/users",
            "status": "agreed",
            "auth": "jwt",
        }
    ]
    audit_routes = [
        {
            "method": "GET",
            "path": "/users",
            "auth": "jwt",
        }
    ]
    fe_endpoints = [
        {
            "method": "GET",
            "path": "/users",
            "source": "handlers.ts",
        }
    ]
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)
    assert findings == []


def test_unbacked_contract_agreed_is_blocking() -> None:
    """Agreed contract with no matching audit route => blocking unbacked_contract."""
    contracts = [
        {
            "id": 10,
            "method": "GET",
            "path": "/users/me/notification-preferences",
            "status": "agreed",
        }
    ]
    audit_routes = []
    fe_endpoints = []
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    assert len(findings) == 1
    assert findings[0]["category"] == "unbacked_contract"
    assert findings[0]["severity"] == "blocking"
    assert findings[0]["contract_id"] == 10
    assert findings[0]["method"] == "GET"
    assert findings[0]["path"] == "/users/me/notification-preferences"


def test_unbacked_contract_nonactive_is_advisory() -> None:
    """Proposed contract with no audit route => advisory unbacked_contract."""
    contracts = [
        {
            "id": 10,
            "method": "GET",
            "path": "/users/me/notification-preferences",
            "status": "proposed",
        }
    ]
    audit_routes = []
    fe_endpoints = []
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    assert len(findings) == 1
    assert findings[0]["category"] == "unbacked_contract"
    assert findings[0]["severity"] == "advisory"
    assert findings[0]["contract_id"] == 10


def test_missing_contract_advisory() -> None:
    """Audit route with no active contract => advisory missing_contract."""
    contracts = []
    audit_routes = [
        {
            "method": "GET",
            "path": "/data/items",
            "auth": "jwt",
        }
    ]
    fe_endpoints = []
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    assert len(findings) == 1
    assert findings[0]["category"] == "missing_contract"
    assert findings[0]["severity"] == "advisory"
    assert findings[0]["contract_id"] is None
    assert findings[0]["method"] == "GET"
    assert findings[0]["path"] == "/data/items"


def test_auth_mismatch_advisory() -> None:
    """Active contract auth vs route auth differ => advisory auth_mismatch."""
    contracts = [
        {
            "id": 2,
            "method": "GET",
            "path": "/items",
            "status": "agreed",
            "auth": "none",
        }
    ]
    audit_routes = [
        {
            "method": "GET",
            "path": "/items",
            "auth": "jwt",
        }
    ]
    fe_endpoints = []
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    assert len(findings) == 1
    assert findings[0]["category"] == "auth_mismatch"
    assert findings[0]["severity"] == "advisory"
    assert findings[0]["contract_id"] == 2


def test_path_normalization_drift_advisory() -> None:
    """Active contract /x/{id} vs route /x/:id (same normalized, differ raw)
    => advisory path_normalization_drift."""
    contracts = [
        {
            "id": 3,
            "method": "GET",
            "path": "/items/{id}",
            "status": "agreed",
            "auth": "jwt",
        }
    ]
    audit_routes = [
        {
            "method": "GET",
            "path": "/items/:id",
            "auth": "jwt",
        }
    ]
    fe_endpoints = []
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    # Should have path_normalization_drift (raw differ) but NOT unbacked_contract
    # or missing_contract (they match post-normalize)
    by_cat = findings_by_category(findings)
    assert "path_normalization_drift" in by_cat
    assert len(by_cat["path_normalization_drift"]) == 1
    assert by_cat["path_normalization_drift"][0]["severity"] == "advisory"
    assert "unbacked_contract" not in by_cat
    assert "missing_contract" not in by_cat


def test_fe_calls_superseded_blocking() -> None:
    """FE endpoint maps to superseded contract => blocking fe_calls_dead_contract."""
    contracts = [
        {
            "id": 3,
            "method": "GET",
            "path": "/notifications",
            "status": "superseded",
        },
        {
            "id": 4,
            "method": "PATCH",
            "path": "/notifications/:id/read",
            "status": "superseded",
        },
    ]
    audit_routes = []
    fe_endpoints = [
        {
            "method": None,
            "path": "/notifications",
            "source": "endpoints.ts",
        },
        {
            "method": None,
            "path": "/notifications/{id}/read",
            "source": "endpoints.ts",
        },
    ]
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    # Should have two fe_calls_dead_contract findings, both blocking
    by_cat = findings_by_category(findings)
    assert "fe_calls_dead_contract" in by_cat
    dead_findings = by_cat["fe_calls_dead_contract"]
    assert len(dead_findings) == 2
    for f in dead_findings:
        assert f["severity"] == "blocking"
        assert f["contract_id"] in {3, 4}


def test_fe_unbacked_path_advisory() -> None:
    """FE ref to path with no contract at all => advisory fe_unbacked_path."""
    contracts = []
    audit_routes = []
    fe_endpoints = [
        {
            "method": "GET",
            "path": "/mystery",
            "source": "handlers.ts",
        }
    ]
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    assert len(findings) == 1
    assert findings[0]["category"] == "fe_unbacked_path"
    assert findings[0]["severity"] == "advisory"
    assert findings[0]["contract_id"] is None


def test_fe_maps_to_active_no_finding() -> None:
    """FE ref to an active contract => no FE finding."""
    contracts = [
        {
            "id": 5,
            "method": "POST",
            "path": "/items",
            "status": "agreed",
        }
    ]
    audit_routes = []
    fe_endpoints = [
        {
            "method": "POST",
            "path": "/items",
            "source": "handlers.ts",
        }
    ]
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    # Only unbacked_contract (no route) but NO FE finding
    by_cat = findings_by_category(findings)
    assert "fe_unbacked_path" not in by_cat
    assert "fe_calls_dead_contract" not in by_cat
    # Should still have unbacked_contract since no audit route
    assert len(by_cat.get("unbacked_contract", [])) == 1


def test_duplicate_active_blocking() -> None:
    """Two active contracts same _key => blocking duplicate_active."""
    contracts = [
        {
            "id": 6,
            "method": "GET",
            "path": "/data",
            "status": "agreed",
        },
        {
            "id": 7,
            "method": "GET",
            "path": "/data",
            "status": "live",
        },
    ]
    audit_routes = []
    fe_endpoints = []
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    by_cat = findings_by_category(findings)
    assert "duplicate_active" in by_cat
    dup_findings = by_cat["duplicate_active"]
    assert len(dup_findings) == 1
    assert dup_findings[0]["severity"] == "blocking"
    # Detail should mention both ids
    assert "6" in dup_findings[0]["detail"]
    assert "7" in dup_findings[0]["detail"]


def test_excluded_routes_never_reported() -> None:
    """Routes in exclusion set => never reported in any category."""
    contracts = [
        {
            "id": 8,
            "method": "GET",
            "path": "/notifications",
            "status": "agreed",
        }
    ]
    audit_routes = []
    fe_endpoints = [
        {
            "method": None,
            "path": "/notifications",
            "source": "endpoints.ts",
        }
    ]
    # Exclude this exact route
    excluded = {("GET", "/notifications")}

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    # Should have NO findings (excluded)
    assert findings == []


def test_finding_shape_and_ordering() -> None:
    """Every finding has exactly the six keys; blocking before advisory."""
    contracts = [
        {
            "id": 1,
            "method": "GET",
            "path": "/items",
            "status": "proposed",
        },
        {
            "id": 2,
            "method": "GET",
            "path": "/users",
            "status": "agreed",
        },
    ]
    audit_routes = [
        {
            "method": "GET",
            "path": "/items",
        }
    ]
    fe_endpoints = []
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    # Should have two findings: one blocking (unbacked /users), one advisory (unbacked /items)
    assert len(findings) == 2

    # Check shape of each
    for f in findings:
        assert set(f.keys()) == {"category", "severity", "method", "path", "contract_id", "detail"}
        assert f["severity"] in ("blocking", "advisory")

    # Check ordering: blocking first
    assert findings[0]["severity"] == "blocking"
    assert findings[1]["severity"] == "advisory"


def test_fe_method_less_refs_match_by_path_only() -> None:
    """Method-less FE refs (from endpoints.ts) match by normalized path against any method."""
    contracts = [
        {
            "id": 10,
            "method": "PATCH",
            "path": "/items/{id}/update",
            "status": "agreed",
        }
    ]
    audit_routes = []
    fe_endpoints = [
        {
            "method": None,
            "path": "/items/{id}/update",
            "source": "endpoints.ts",
        }
    ]
    excluded = set()

    findings = diff_contracts(contracts, audit_routes, fe_endpoints, excluded)

    # FE should match the contract by normalized path, giving only unbacked_contract
    by_cat = findings_by_category(findings)
    assert "unbacked_contract" in by_cat
    assert "fe_unbacked_path" not in by_cat


# ============================================================================ #
# Tests: parsers
# ============================================================================ #


def test_parse_endpoint_registry() -> None:
    """endpoints.ts parser extracts path literals and rewrites ${...} -> {id}."""
    text = """
    export const NOTIFICATIONS = {
      list: '/notifications',
      markRead: (id: string) => `/notifications/${id}/read`,
    };
    export const ITEMS = {
      list: "/items",
      detail: (id: string) => `/items/${id}`,
    };
    """

    endpoints = parse_endpoint_registry(text)

    # Should extract 4 unique paths
    assert len(endpoints) == 4
    paths = {e["path"] for e in endpoints}
    assert "/notifications" in paths
    assert "/notifications/{id}/read" in paths
    assert "/items" in paths
    assert "/items/{id}" in paths

    # All should have method=None and source='endpoints.ts'
    for e in endpoints:
        assert e["method"] is None
        assert e["source"] == "endpoints.ts"


def test_parse_msw_handlers() -> None:
    """handlers.ts parser extracts method and path from http.<verb> calls."""
    text = """
    import { http } from 'msw';
    export const handlers = [
      http.get(`${API}/users/me/notifications`, ({ request }) => ...),
      http.patch(`${API}/users/me/notifications/:id`, ({ request }) => ...),
      http.post(`${API}/items`, ({ request }) => ...),
    ];
    """

    handlers = parse_msw_handlers(text)

    assert len(handlers) == 3

    # Check the routes we parsed
    methods_and_paths = {(h["method"], h["path"]) for h in handlers}
    assert ("GET", "/users/me/notifications") in methods_and_paths
    assert ("PATCH", "/users/me/notifications/:id") in methods_and_paths
    assert ("POST", "/items") in methods_and_paths

    # All should have source='handlers.ts'
    for h in handlers:
        assert h["source"] == "handlers.ts"


def test_parse_endpoint_registry_dedupes() -> None:
    """endpoints.ts parser dedupes identical paths."""
    text = """
    export const A = { path: '/items' };
    export const B = { path: '/items' };
    export const C = { path: '/items' };
    """

    endpoints = parse_endpoint_registry(text)

    # Should dedupe to one
    assert len(endpoints) == 1
    assert endpoints[0]["path"] == "/items"


def test_parse_msw_handlers_dedupes() -> None:
    """handlers.ts parser dedupes identical (method, path) pairs."""
    text = """
    http.get(`${API}/items`, () => ...),
    http.get(`${API}/items`, () => ...),
    http.post(`${API}/items`, () => ...),
    """

    handlers = parse_msw_handlers(text)

    # Should have 2 unique: GET /items and POST /items
    assert len(handlers) == 2


def test_parse_endpoint_registry_ignores_non_paths() -> None:
    """endpoints.ts parser ignores non-path string literals."""
    text = """
    const name = "myapp";
    export const ENDPOINTS = {
      list: '/items',
      name: 'MyApp',
      items: '/data/items',
    };
    """

    endpoints = parse_endpoint_registry(text)

    paths = {e["path"] for e in endpoints}
    assert "/items" in paths
    assert "/data/items" in paths
    assert "MyApp" not in paths  # Should not include non-path string
    assert "myapp" not in paths


def test_parse_msw_handlers_case_insensitive() -> None:
    """handlers.ts parser is case-insensitive for http.<verb>."""
    text = """
    http.GET(`${API}/items`, () => ...),
    http.Post(`${API}/items`, () => ...),
    """

    handlers = parse_msw_handlers(text)

    methods = {h["method"] for h in handlers}
    assert "GET" in methods
    assert "POST" in methods
