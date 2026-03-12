"""Tests for path validation and repo-scope enforcement.

Covers: _resolve_candidate_path, _is_within_repo, _iter_tool_paths, _repo_scope_violation
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chatter import bot

# ---------------------------------------------------------------------------
# _resolve_candidate_path
# ---------------------------------------------------------------------------

class TestResolveCandidatePath:
    def test_absolute_path_returned_as_is(self, repo_root: Path):
        result = bot._resolve_candidate_path("/usr/bin/python", repo_root)
        assert result == Path("/usr/bin/python").resolve()

    def test_relative_path_joined_to_repo(self, repo_root: Path):
        result = bot._resolve_candidate_path("src/main.py", repo_root)
        assert result == (repo_root / "src" / "main.py").resolve()

    def test_tilde_expanded(self, repo_root: Path):
        result = bot._resolve_candidate_path("~/somefile", repo_root)
        expected = Path.home() / "somefile"
        assert result == expected.resolve()

    def test_whitespace_stripped(self, repo_root: Path):
        result = bot._resolve_candidate_path("  src/main.py  ", repo_root)
        assert result == (repo_root / "src" / "main.py").resolve()

    def test_dot_path(self, repo_root: Path):
        result = bot._resolve_candidate_path(".", repo_root)
        assert result == repo_root.resolve()


# ---------------------------------------------------------------------------
# _is_within_repo
# ---------------------------------------------------------------------------

class TestIsWithinRepo:
    def test_path_inside_repo(self, repo_root: Path):
        assert bot._is_within_repo(repo_root / "src" / "main.py", repo_root)

    def test_repo_root_itself(self, repo_root: Path):
        assert bot._is_within_repo(repo_root, repo_root)

    def test_path_outside_repo(self, repo_root: Path):
        assert not bot._is_within_repo(Path("/etc/passwd"), repo_root)

    def test_parent_of_repo(self, repo_root: Path):
        assert not bot._is_within_repo(repo_root.parent, repo_root)

    def test_sibling_directory(self, repo_root: Path):
        sibling = repo_root.parent / "other-repo"
        assert not bot._is_within_repo(sibling, repo_root)


# ---------------------------------------------------------------------------
# _iter_tool_paths
# ---------------------------------------------------------------------------

class TestIterToolPaths:
    def test_extracts_file_path(self):
        paths = bot._iter_tool_paths("Read", {"file_path": "/foo/bar.py"})
        assert "/foo/bar.py" in paths

    def test_extracts_path(self):
        paths = bot._iter_tool_paths("Edit", {"path": "/foo/baz.py"})
        assert "/foo/baz.py" in paths

    def test_extracts_cwd(self):
        paths = bot._iter_tool_paths("Bash", {"cwd": "/some/dir", "command": "ls"})
        assert "/some/dir" in paths

    def test_extracts_notebook_path(self):
        paths = bot._iter_tool_paths("NotebookEdit", {"notebook_path": "/a/b.ipynb"})
        assert "/a/b.ipynb" in paths

    def test_extracts_path_lists(self):
        paths = bot._iter_tool_paths("SomeTool", {"file_paths": ["/a.py", "/b.py"]})
        assert "/a.py" in paths
        assert "/b.py" in paths

    def test_ignores_empty_strings(self):
        paths = bot._iter_tool_paths("Read", {"file_path": "  "})
        assert len(paths) == 0

    def test_glob_pattern_extracts_anchor(self):
        paths = bot._iter_tool_paths("Glob", {"pattern": "src/**/*.py"})
        assert "src/" in paths

    def test_glob_pattern_wildcard_start(self):
        paths = bot._iter_tool_paths("Glob", {"pattern": "**/*.py"})
        # Anchor should be "." for patterns starting with glob chars
        assert "." in paths

    def test_no_relevant_keys(self):
        paths = bot._iter_tool_paths("WebSearch", {"query": "hello"})
        assert len(paths) == 0


# ---------------------------------------------------------------------------
# _repo_scope_violation (integration of the above)
# ---------------------------------------------------------------------------

class TestRepoScopeViolation:
    @pytest.fixture(autouse=True)
    def _setup_config(self, mock_bot_config, repo_root):
        """Inject a mock config so _repo_root() works."""
        original = bot._config
        bot._config = mock_bot_config
        yield
        bot._config = original

    def test_read_inside_repo_ok(self, repo_root: Path):
        result = bot._repo_scope_violation(
            "Read", {"file_path": str(repo_root / "src" / "main.py")}
        )
        assert result is None

    def test_read_outside_repo_blocked(self, repo_root: Path):
        result = bot._repo_scope_violation("Read", {"file_path": "/etc/passwd"})
        assert result is not None
        assert "outside" in result.lower()

    def test_bash_inside_repo_ok(self, repo_root: Path):
        result = bot._repo_scope_violation("Bash", {"command": "ls src/"})
        assert result is None

    def test_bash_outside_repo_blocked(self, repo_root: Path):
        result = bot._repo_scope_violation("Bash", {"command": "cd /etc"})
        assert result is not None

    def test_glob_inside_repo_ok(self, repo_root: Path):
        result = bot._repo_scope_violation(
            "Glob", {"pattern": "src/**/*.py", "path": str(repo_root)}
        )
        assert result is None

    def test_glob_outside_repo_blocked(self, repo_root: Path):
        result = bot._repo_scope_violation(
            "Glob", {"pattern": "/etc/**/*", "path": "/etc"}
        )
        assert result is not None

    def test_edit_inside_repo_ok(self, repo_root: Path):
        result = bot._repo_scope_violation(
            "Edit", {"file_path": str(repo_root / "README.md")}
        )
        assert result is None

    def test_edit_outside_repo_blocked(self):
        result = bot._repo_scope_violation(
            "Edit", {"file_path": "/tmp/evil.py"}
        )
        assert result is not None

    def test_websearch_no_paths(self):
        """Tools with no path keys should never violate."""
        result = bot._repo_scope_violation("WebSearch", {"query": "hello"})
        assert result is None
