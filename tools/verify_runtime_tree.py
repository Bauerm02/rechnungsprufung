"""Release gate: code provenance only; no database, jobs, mail or bank calls."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*.py"))}


def verify(expected, installed_root, scripts_root):
    installed_root = Path(installed_root).resolve()
    scripts_root = Path(scripts_root).resolve()
    if (scripts_root.parent / "src" / "mietinkasso").exists():
        raise RuntimeError("Obsolete runtime source tree remains")
    if hashes(installed_root) != expected["package"]:
        raise RuntimeError("Installed package differs from reviewed release manifest")
    for name in ["indexautomatik_taegliche_pflege.py", "indexautomatik_monatslauf.py"]:
        script = scripts_root / name
        if hashlib.sha256(script.read_bytes()).hexdigest() != expected["scripts"][name]:
            raise RuntimeError("Timer CLI differs from reviewed release manifest")
        tree = ast.parse(script.read_text(encoding="utf-8"))
        prefix = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mietinkasso"):
                break
            prefix.append(node)
        namespace = {"__name__": "release_import_probe", "__file__": str(script)}
        previous_path = sys.path[:]
        try:
            exec(compile(ast.Module(body=prefix, type_ignores=[]), str(script), "exec"), namespace)
            spec = importlib.util.find_spec("mietinkasso")
            if spec is None or Path(spec.origin).resolve() != installed_root / "__init__.py":
                raise RuntimeError("Timer CLI selects a different package")
            # find_spec checks availability, without executing the service or main.
            spec = importlib.util.find_spec("mietinkasso.eigentuemerzahlungen.service")
            if spec is None or not Path(spec.origin).resolve().is_relative_to(installed_root):
                raise RuntimeError("Required owner-payment module is unavailable")
        finally:
            sys.path[:] = previous_path
    return {"package_files": len(expected["package"]), "cli_entrypoints": 2, "business_actions": 0}


if __name__ == "__main__":
    expected = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    spec = importlib.util.find_spec("mietinkasso")
    if spec is None or not spec.origin:
        raise RuntimeError("Installed package missing")
    print(json.dumps(verify(expected, Path(spec.origin).parent, "/app/scripts")))
