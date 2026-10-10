"""Synthetic import selection, never run a business job or open a database."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS = ["indexautomatik_taegliche_pflege.py", "indexautomatik_monatslauf.py"]
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("installed,checkout", [(True, True), (True, False), (False, True), (False, False)])
def test_cli_selects_one_source_without_business_execution(tmp_path, script, installed, checkout):
    # Execute only the import-selection prefix; do not import the CLI's business
    # modules. -S and an empty PYTHONPATH exclude the developer installation.
    real = ROOT / "scripts" / script
    tree = ast.parse(real.read_text(encoding="utf-8"))
    bootstrap = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mietinkasso"):
            break
        bootstrap.append(node)
    copied = tmp_path / "checkout" / "scripts" / script
    copied.parent.mkdir(parents=True)
    copied.write_text(ast.unparse(ast.Module(body=bootstrap, type_ignores=[])), encoding="utf-8")
    installed_root = tmp_path / "installed"
    for enabled, root, marker in [(installed, installed_root, "installed"), (checkout, copied.parent.parent / "src", "checkout")]:
        if enabled:
            package = root / "mietinkasso"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("MARKER = " + repr(marker), encoding="utf-8")
    probe = """
import runpy, sys, json
sys.path.insert(0, sys.argv[2])
runpy.run_path(sys.argv[1], run_name='import_probe')
try:
    import mietinkasso
except ModuleNotFoundError:
    print(json.dumps({'marker': None}))
else:
    print(json.dumps({'marker': mietinkasso.MARKER}))
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", probe, str(copied), str(installed_root)],
                            cwd=tmp_path, env={**os.environ, "PYTHONPATH": ""}, capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(result.stdout)["marker"] == ("installed" if installed else "checkout" if checkout else None)


@pytest.mark.parametrize("script", SCRIPTS)
def test_main_guard_present_and_bootstrap_does_not_hide_errors(script):
    tree = ast.parse((ROOT / "scripts" / script).read_text(encoding="utf-8"))
    assert isinstance(tree.body[-1], ast.If)
    assert ast.unparse(tree.body[-1].test) == "__name__ == '__main__'"
    helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_ensure_mietinkasso_importable")
    assert not any(isinstance(n, ast.Try) for n in ast.walk(helper))


@pytest.mark.parametrize("script", SCRIPTS)
def test_installed_package_error_is_not_hidden_by_checkout(tmp_path, script):
    tree = ast.parse((ROOT / "scripts" / script).read_text(encoding="utf-8"))
    helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_ensure_mietinkasso_importable")
    installed = tmp_path / "installed" / "mietinkasso"
    installed.mkdir(parents=True)
    (installed / "__init__.py").write_text("import dependency_that_does_not_exist_001", encoding="utf-8")
    checkout = tmp_path / "src" / "mietinkasso"
    checkout.mkdir(parents=True)
    (checkout / "__init__.py").write_text("MARKER = 'incorrect_fallback'", encoding="utf-8")
    probe = "import sys\nfrom pathlib import Path\n"
    probe += "sys.path.insert(0, " + repr(str(installed.parent)) + ")\n"
    probe += "__file__ = " + repr(str(tmp_path / "scripts" / script)) + "\n"
    probe += ast.unparse(helper) + "\n_ensure_mietinkasso_importable()\nimport mietinkasso\n"
    result = subprocess.run([sys.executable, "-I", "-S", "-c", probe], env={**os.environ, "PYTHONPATH": ""},
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "dependency_that_does_not_exist_001" in result.stderr
