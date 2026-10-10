import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mutation", ["none", "extra", "changed", "missing", "shadow", "cli", "pythonpath", "missing_manifest", "nested_dependency", "missing_symbol"])
def test_runtime_gate_detects_source_drift_without_job_execution(tmp_path, mutation):
    package = tmp_path / "installed" / "mietinkasso"
    child = package / "eigentuemerzahlungen"
    child.mkdir(parents=True)
    for p in [package / "__init__.py", child / "__init__.py", child / "service.py"]:
        p.write_text("# synthetic marker\n", encoding="utf-8")
    (child / "service.py").write_text("def generate(): raise RuntimeError('must never call business function')\n", encoding="utf-8")
    scripts = tmp_path / "app" / "scripts"
    scripts.mkdir(parents=True)
    for name in ["indexautomatik_taegliche_pflege.py", "indexautomatik_monatslauf.py"]:
        tree = ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))
        prefix = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mietinkasso"):
                break
            prefix.append(node)
        (scripts / name).write_text(ast.unparse(ast.Module(body=prefix, type_ignores=[])) +
            "\ndef main():\n    from mietinkasso.eigentuemerzahlungen.service import generate\n    generate()\n", encoding="utf-8")
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    expected = {"package": {p.relative_to(package).as_posix(): digest(p) for p in package.rglob("*.py")},
                "scripts": {p.name: digest(p) for p in scripts.glob("*.py")}}
    if mutation == "extra": (package / "deleted_plugin.py").write_text("# old", encoding="utf-8")
    if mutation == "changed": (child / "service.py").write_text("# old", encoding="utf-8")
    if mutation == "missing": (child / "service.py").unlink()
    if mutation == "cli": (scripts / "indexautomatik_monatslauf.py").write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    if mutation == "shadow": (scripts.parent / "src" / "mietinkasso").mkdir(parents=True)
    if mutation == "missing_manifest":
        (child / "service.py").unlink()
        del expected["package"]["eigentuemerzahlungen/service.py"]
    if mutation == "nested_dependency":
        (child / "service.py").write_text("import missing_synthetic_dependency_001", encoding="utf-8")
        expected["package"]["eigentuemerzahlungen/service.py"] = digest(child / "service.py")
    if mutation == "missing_symbol":
        (child / "service.py").write_text("# generate symbol missing", encoding="utf-8")
        expected["package"]["eigentuemerzahlungen/service.py"] = digest(child / "service.py")
    wrong = tmp_path / "wrong" / "mietinkasso"
    if mutation == "pythonpath":
        wrong.mkdir(parents=True)
        (wrong / "__init__.py").write_text("# obsolete", encoding="utf-8")
    probe = """
import sys, importlib.util, json
sys.path.insert(0, sys.argv[2])
if sys.argv[5] == 'pythonpath': sys.path.insert(0, sys.argv[6])
spec=importlib.util.spec_from_file_location('runtime_gate', sys.argv[1])
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
result=mod.verify(json.loads(sys.argv[4]), sys.argv[2]+'/mietinkasso', sys.argv[3])
print(json.dumps(result))
"""
    result = subprocess.run([sys.executable, "-S", "-c", probe, str(ROOT / "tools" / "verify_runtime_tree.py"),
                             str(package.parent), str(scripts), json.dumps(expected), mutation, str(wrong.parent)],
                            env={**os.environ, "PYTHONPATH": ""}, capture_output=True, text=True, timeout=10)
    if mutation == "none":
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["verification"] == "imports_only"
    else:
        assert result.returncode != 0
        assert "RuntimeError" in result.stderr or "ImportError" in result.stderr or "ModuleNotFoundError" in result.stderr
