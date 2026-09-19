"""HTTP regression contract, checked in a fresh process with a private empty DB.

Importing the application during collection would capture another test's
configuration. Keep the complete runtime probe in its own process.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_PROBE = r'''
import hashlib, json, re
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from mietinkasso.api.app import app

def effective_routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif callable(getattr(route, "effective_route_contexts", None)):
            yield from route.effective_route_contexts()

def dependencies(dep):
    return [{"call":getattr(child.call,"__name__",None),
             "children":dependencies(child)} for child in dep.dependencies]

routes = [r for r in effective_routes(app.routes) if r.path.startswith("/backoffice")]
contracts, closed, sessions = [], [], set()
with TestClient(app, follow_redirects=False) as client:
    for route in routes:
        url = re.sub(r"\{[^}]+\}", "synthetic-missing-id", route.path)
        for method in sorted(route.methods):
            winner = next(r for r in routes if method in r.methods and r.path_regex.match(url))
            contracts.append({"path":route.path,"method":method,"endpoint":route.endpoint.__name__,
                              "resolved_endpoint":winner.endpoint.__name__,
                              "dependencies":dependencies(route.dependant)})
            response = client.request(method, url)
            closed.append({"path":route.path,"method":method.lower(),"status":response.status_code,
                           "location":response.headers.get("location"),
                           "body_sha256":hashlib.sha256(response.content).hexdigest()})
        pending = list(route.dependant.dependencies)
        while pending:
            dep = pending.pop()
            if getattr(dep.call,"__name__",None) == "_current_session":
                sessions.add(id(dep.call))
            pending.extend(dep.dependencies)
key = lambda row:(row["path"],row["method"])
print(json.dumps({"routes":sorted(contracts,key=key),"closed":sorted(closed,key=key),
                  "session_dependency_count":len(sessions)}))
'''


@pytest.fixture(scope="module")
def contract_result(tmp_path_factory):
    temporary = tmp_path_factory.mktemp("backoffice-http-contract")
    root = Path(__file__).resolve().parents[2]
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("MIETINKASSO_")
    }
    env.update(
        MIETINKASSO_DATABASE_URL=f"sqlite:///{temporary / 'synthetic.sqlite'}",
        MIETINKASSO_ENVIRONMENT="test",
        MIETINKASSO_BACKOFFICE_PASSWORD_HASH="",
        MIETINKASSO_API_TOKEN="",
        MIETINKASSO_SEND_ENABLED="false",
        MIETINKASSO_INDEXAUTOMATIK_SEND_ENABLED="false",
        MIETINKASSO_INDEX_MONATSBERICHT_SEND_ENABLED="false",
        MIETINKASSO_VERTRAGSENDE_ERINNERUNG_SEND_ENABLED="false",
    )
    # Preserve installed test dependencies while choosing this exact worktree.
    env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=root, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=45,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def expected_contract():
    fixture = Path(__file__).with_name("fixtures") / "backoffice_http_contract.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_routes_parameters_resolution_and_auth_remain_compatible(contract_result, expected_contract):
    assert contract_result["routes"] == expected_contract["routes"]


def test_all_backoffice_routes_remain_closed_without_configuration(contract_result, expected_contract):
    assert contract_result["closed"] == expected_contract["closed"]


def test_protected_routes_use_one_shared_session_dependency(contract_result):
    assert contract_result["session_dependency_count"] == 1
