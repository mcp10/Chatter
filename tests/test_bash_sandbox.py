"""Tests for bash command sandboxing (_bash_within_repo).

This is the most security-critical test file. The bash validator must catch
all attempts to escape the repository boundary via shell commands.
"""

from __future__ import annotations

from pathlib import Path

from chatter.bot import _bash_within_repo


class TestBashAllowed:
    """Commands that should be ALLOWED."""

    def test_empty_command(self, repo_root: Path):
        ok, reason = _bash_within_repo("", repo_root)
        assert ok
        assert reason is None

    def test_simple_ls(self, repo_root: Path):
        ok, _ = _bash_within_repo("ls", repo_root)
        assert ok

    def test_ls_dot(self, repo_root: Path):
        ok, _ = _bash_within_repo("ls .", repo_root)
        assert ok

    def test_cat_relative_file(self, repo_root: Path):
        ok, _ = _bash_within_repo("cat ./file.txt", repo_root)
        assert ok

    def test_cat_subdir_file(self, repo_root: Path):
        ok, _ = _bash_within_repo("cat src/main.py", repo_root)
        assert ok

    def test_cd_relative_subdir(self, repo_root: Path):
        """cd with a relative path inside the repo should be allowed."""
        ok, _ = _bash_within_repo("cd src", repo_root)
        assert ok

    def test_cd_absolute_inside_repo_blocked(self, repo_root: Path):
        """cd with absolute path is blocked even if inside repo (intentional heuristic).

        The regex blocks all 'cd /...' patterns as a fast safety check.
        """
        subdir = repo_root / "src"
        ok, _ = _bash_within_repo(f"cd {subdir}", repo_root)
        assert not ok  # Intentional: cd + absolute path always blocked

    def test_url_with_absolute_path(self, repo_root: Path):
        """URLs should NOT be treated as filesystem paths."""
        ok, _ = _bash_within_repo("curl https://example.com/api/v1/data", repo_root)
        assert ok

    def test_url_in_wget(self, repo_root: Path):
        ok, _ = _bash_within_repo("wget http://example.com/file.tar.gz", repo_root)
        assert ok

    def test_git_commands(self, repo_root: Path):
        ok, _ = _bash_within_repo("git status", repo_root)
        assert ok

    def test_pipe_within_repo(self, repo_root: Path):
        ok, _ = _bash_within_repo("cat src/main.py | grep import", repo_root)
        assert ok

    def test_absolute_path_inside_repo(self, repo_root: Path):
        ok, _ = _bash_within_repo(f"cat {repo_root}/src/main.py", repo_root)
        assert ok

    def test_python_with_repo_path(self, repo_root: Path):
        ok, _ = _bash_within_repo(f"python {repo_root}/src/main.py", repo_root)
        assert ok

    def test_echo_simple(self, repo_root: Path):
        ok, _ = _bash_within_repo("echo hello world", repo_root)
        assert ok


class TestBashBlocked:
    """Commands that must be BLOCKED."""

    def test_cd_root(self, repo_root: Path):
        ok, reason = _bash_within_repo("cd /", repo_root)
        assert not ok
        assert reason is not None

    def test_cd_etc(self, repo_root: Path):
        ok, reason = _bash_within_repo("cd /etc", repo_root)
        assert not ok

    def test_cd_home(self, repo_root: Path):
        ok, reason = _bash_within_repo("cd ~", repo_root)
        assert not ok

    def test_cd_dotdot(self, repo_root: Path):
        ok, reason = _bash_within_repo("cd ..", repo_root)
        assert not ok

    def test_cd_dotdot_multiple(self, repo_root: Path):
        ok, reason = _bash_within_repo("cd ../..", repo_root)
        assert not ok

    def test_dotdot_traversal_in_path(self, repo_root: Path):
        ok, reason = _bash_within_repo("cat ../../etc/passwd", repo_root)
        assert not ok

    def test_dotdot_in_middle_of_path(self, repo_root: Path):
        ok, reason = _bash_within_repo("cat src/../../etc/passwd", repo_root)
        assert not ok

    def test_absolute_path_outside_repo(self, repo_root: Path):
        ok, reason = _bash_within_repo("cat /etc/passwd", repo_root)
        assert not ok

    def test_embedded_abs_path_in_python(self, repo_root: Path):
        ok, reason = _bash_within_repo("python -c \"open('/etc/passwd')\"", repo_root)
        assert not ok

    def test_tilde_path_escape(self, repo_root: Path):
        ok, reason = _bash_within_repo("cat ~/secret.txt", repo_root)
        assert not ok

    def test_semicolon_cd_escape(self, repo_root: Path):
        ok, reason = _bash_within_repo("ls; cd /etc", repo_root)
        assert not ok

    def test_and_cd_escape(self, repo_root: Path):
        ok, reason = _bash_within_repo("ls && cd /tmp", repo_root)
        assert not ok

    def test_pipe_then_cd_escape(self, repo_root: Path):
        ok, reason = _bash_within_repo("echo x | cd /etc", repo_root)
        assert not ok

    def test_rm_outside_repo(self, repo_root: Path):
        ok, reason = _bash_within_repo("rm /tmp/important", repo_root)
        assert not ok

    def test_cp_to_outside(self, repo_root: Path):
        ok, reason = _bash_within_repo(f"cp {repo_root}/file.txt /tmp/", repo_root)
        assert not ok

    def test_dotdot_as_standalone_token(self, repo_root: Path):
        ok, reason = _bash_within_repo("ls ..", repo_root)
        assert not ok

    def test_path_ending_with_dotdot(self, repo_root: Path):
        ok, reason = _bash_within_repo("ls src/..", repo_root)
        assert not ok


class TestBashEdgeCases:
    """Edge cases and tricky inputs."""

    def test_whitespace_only(self, repo_root: Path):
        ok, _ = _bash_within_repo("   ", repo_root)
        assert ok

    def test_command_with_equals_and_path(self, repo_root: Path):
        """Environment variable assignment with outside path."""
        ok, reason = _bash_within_repo("PATH=/usr/bin python", repo_root)
        assert not ok

    def test_quoted_path_with_spaces(self, repo_root: Path):
        ok, _ = _bash_within_repo(f'cat "{repo_root}/src/main.py"', repo_root)
        assert ok

    def test_chained_safe_commands(self, repo_root: Path):
        ok, _ = _bash_within_repo("echo hello && echo world", repo_root)
        assert ok

    def test_redirect_inside_repo(self, repo_root: Path):
        ok, _ = _bash_within_repo(f"echo test > {repo_root}/output.txt", repo_root)
        assert ok

    def test_very_long_command(self, repo_root: Path):
        """Ensure no performance issues with long commands."""
        ok, _ = _bash_within_repo("echo " + "a" * 10000, repo_root)
        assert ok

    def test_null_bytes_in_command(self, repo_root: Path):
        """Should handle gracefully even with unusual input."""
        # shlex.split may raise, but the function should not crash
        ok, reason = _bash_within_repo("ls\x00/etc", repo_root)
        # Either blocked or allowed, just don't crash
        assert isinstance(ok, bool)
