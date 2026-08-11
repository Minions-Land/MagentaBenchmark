from __future__ import annotations

from pathlib import Path
import subprocess

from MagentaBench.collab.cli import _git_changed_paths


def test_git_changed_paths_uses_merge_base_for_diverged_branches(tmp_path: Path) -> None:
    def git(*arguments: str) -> None:
        subprocess.run(
            ("git", "-C", str(tmp_path), *arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    git("init", "--initial-branch=main")
    git("config", "user.name", "MagentaBench Test")
    git("config", "user.email", "magentabench-test@example.invalid")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "base.txt")
    git("commit", "-m", "base")

    git("switch", "-c", "experiment")
    bundle = tmp_path / "experiments/example/bundle.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("{}\n", encoding="utf-8")
    git("add", bundle.relative_to(tmp_path).as_posix())
    git("commit", "-m", "experiment")

    git("switch", "main")
    protocol = tmp_path / "MagentaBench/schemas/models.py"
    protocol.parent.mkdir(parents=True)
    protocol.write_text("# main-only change\n", encoding="utf-8")
    git("add", protocol.relative_to(tmp_path).as_posix())
    git("commit", "-m", "main protocol change")

    assert _git_changed_paths(tmp_path, "main", "experiment") == (
        "experiments/example/bundle.json",
    )
