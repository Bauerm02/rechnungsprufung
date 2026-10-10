import importlib.util
import json
from pathlib import Path
import subprocess


def test_manifest_uses_committed_bytes_and_ignores_dirty_workspace(tmp_path):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("manifest_builder", root / "tools/build_runtime_manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    package = repo / "src/mietinkasso"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# committed\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    for name in ["indexautomatik_taegliche_pflege.py", "indexautomatik_monatslauf.py"]:
        (repo / "scripts" / name).write_text("# committed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-m", "synthetic fixture"], check=True, capture_output=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (package / "__init__.py").write_text("# dirty workspace\n", encoding="utf-8")
    output = tmp_path / "context"
    result = mod.build_context(repo, commit, output)
    assert (output / "src/mietinkasso/__init__.py").read_text().strip() == "# committed"
    assert result["commit"] == commit
    assert len(result["manifest_sha256"]) == 64
    assert json.loads((output / "runtime-tree-manifest.json").read_text())["commit"] == commit
