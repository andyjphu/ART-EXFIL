"""Mock-only tests for local Gitea provisioning."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge_setup import ForgeSetupError, _anonymous_git_read, _api, _base_url, main, provision_forge


class FakeGitea:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, tuple | None]] = []
        self.admin: tuple[str, str, str] | None = None
        self.public_git_checks: list[str] = []
        self.public = True
        self.permission = "write"
        self.anonymous_git_fails = False

    def create_admin(self, username: str, password: str, email: str) -> None:
        self.admin = username, password, email

    def api(self, base: str, method: str, path: str, payload=None, auth=None, expected=(200,)) -> dict:
        self.calls.append((method, path, payload, auth))
        assert base == "http://127.0.0.1:3000"
        if method == "GET" and path == "/api/v1/user":
            if auth and auth[0] == "bootstrapadmin":
                assert self.admin and auth[1] == self.admin[1]
                return {"login": "bootstrapadmin", "is_admin": True}
            assert auth and auth[0] == "producer"
            return {"login": "producer", "is_admin": False}
        if method == "POST" and path == "/api/v1/admin/users":
            assert expected == (201,)
            return {"login": "producer", "is_admin": False}
        if method == "POST" and path == "/api/v1/orgs":
            return {"username": "finance", "name": "finance"}
        if method == "POST" and path == "/api/v1/orgs/finance/repos":
            assert payload == {"name": "workbook-tools", "private": False, "auto_init": True}
            return {"name": "workbook-tools", "private": False, "owner": {"login": "finance"}}
        if method == "PUT" and path.endswith("/collaborators/producer"):
            assert payload == {"permission": "write"}
            assert expected == (204,)
            return {}
        if method == "GET" and path.endswith("/collaborators/producer/permission"):
            return {"permission": self.permission}
        if method == "POST" and path == "/api/v1/users/producer/tokens":
            assert payload["scopes"] == ["write:repository"]
            assert auth and auth[0] == "producer"
            return {"sha1": "synthetic-test-token", "scopes": ["write:repository"]}
        if method == "GET" and path == "/api/v1/repos/finance/workbook-tools":
            assert auth is None
            return {"name": "workbook-tools", "private": not self.public, "owner": {"login": "finance"}}
        raise AssertionError(f"Unexpected mocked request: {method} {path}")

    def anonymous_git_read(self, url: str) -> None:
        self.public_git_checks.append(url)
        if self.anonymous_git_fails:
            raise ForgeSetupError("Anonymous Git read failed")


class ForgeSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.gitea = FakeGitea()

    def provision(self) -> dict[str, str]:
        return provision_forge(
            self.state,
            create_admin=self.gitea.create_admin,
            api=self.gitea.api,
            anonymous_git_read=self.gitea.anonymous_git_read,
        )

    def test_private_credentials_public_repo_and_writer_scope(self) -> None:
        result = self.provision()
        self.assertEqual(result, {"repo_url": "http://forge:3000/finance/workbook-tools.git"})
        admin_file = self.state / "evaluator" / "forge.json"
        producer_file = self.state / "producer" / "private" / "git-credentials"
        admin = json.loads(admin_file.read_text())
        self.assertEqual(admin["username"], "bootstrapadmin")
        self.assertEqual(admin["password"], self.gitea.admin[1])
        self.assertEqual(producer_file.read_text(), "http://producer:synthetic-test-token@forge:3000\n")
        self.assertEqual(stat.S_IMODE(admin_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(producer_file.stat().st_mode), 0o600)
        for private_dir in (self.state, self.state / "evaluator", self.state / "producer", producer_file.parent):
            self.assertEqual(stat.S_IMODE(private_dir.stat().st_mode), 0o700)
        self.assertEqual(self.gitea.public_git_checks, ["http://127.0.0.1:3000/finance/workbook-tools.git"])
        token_call = next(call for call in self.gitea.calls if call[1].endswith("/tokens"))
        self.assertEqual(token_call[2]["scopes"], ["write:repository"])
        self.assertTrue(any(call[0] == "GET" and call[1].endswith("/permission") for call in self.gitea.calls))
        self.assertNotIn("synthetic-test-token", json.dumps(result))
        self.assertNotIn(self.gitea.admin[1], json.dumps(result))

    def test_fail_closed_when_anonymous_git_is_not_readable(self) -> None:
        self.gitea.anonymous_git_fails = True
        with self.assertRaisesRegex(ForgeSetupError, "Anonymous Git read failed"):
            self.provision()
        self.assertTrue((self.state / "evaluator" / "forge.json").exists())
        self.assertFalse((self.state / "producer" / "private" / "git-credentials").exists())

    def test_fail_closed_for_wrong_collaborator_permission_or_private_repo(self) -> None:
        for condition in ("permission", "public"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as tmp:
                self.state = Path(tmp) / "state"
                self.gitea = FakeGitea()
                if condition == "permission":
                    self.gitea.permission = "admin"
                else:
                    self.gitea.public = False
                with self.assertRaises(ForgeSetupError):
                    self.provision()
                self.assertFalse((self.state / "producer" / "private" / "git-credentials").exists())

    def test_rejects_non_loopback_or_embedded_credentials_before_setup(self) -> None:
        for base in (
            "https://github.com", "http://forge:3000", "http://admin:secret@127.0.0.1:3000",
            "http://127.0.0.1:13000", "http://127.0.0.1:3000/path", "http://127.0.0.1:3000/?token=secret",
        ):
            with self.subTest(base=base), self.assertRaises(ValueError):
                _base_url(base)
        self.assertFalse(self.state.exists())

    def test_existing_state_or_symlink_rejected_without_creating_admin(self) -> None:
        admin_file = self.state / "evaluator" / "forge.json"
        admin_file.parent.mkdir(parents=True)
        admin_file.write_text("old")
        with self.assertRaises(ForgeSetupError):
            self.provision()
        self.assertIsNone(self.gitea.admin)
        admin_file.unlink()
        admin_file.symlink_to(Path(self.tmp.name) / "elsewhere")
        with self.assertRaises(ForgeSetupError):
            self.provision()
        self.assertIsNone(self.gitea.admin)

    def test_api_uses_container_curl_with_secrets_only_on_stdin(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b'{"ok":true}\n201', b"")
        with patch("forge_setup.subprocess.run", return_value=completed) as run:
            result = _api(
                "http://127.0.0.1:3000", "POST", "/api/v1/admin/users",
                {"username": "producer", "password": "new-secret"},
                ("bootstrapadmin", "admin-secret"), (201,),
            )
        self.assertEqual(result, {"ok": True})
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[:6], ["docker", "compose", "exec", "-T", "forge", "curl"])
        self.assertIn("--config", command)
        self.assertNotIn("admin-secret", " ".join(command))
        self.assertNotIn("new-secret", " ".join(command))
        config = kwargs["input"].decode("utf-8")
        self.assertIn('user = "bootstrapadmin:admin-secret"', config)
        self.assertIn("new-secret", config)
        self.assertIn('url = "http://127.0.0.1:3000/api/v1/admin/users"', config)

    def test_container_api_and_git_fail_closed_without_leaking_response(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b'{"message":"private-detail"}\n401', b"")
        with patch("forge_setup.subprocess.run", return_value=completed):
            with self.assertRaises(ForgeSetupError) as caught:
                _api("http://127.0.0.1:3000", "GET", "/api/v1/user", auth=("bootstrapadmin", "private-password"))
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("private-detail", str(caught.exception))
        self.assertNotIn("private-password", str(caught.exception))
        with patch("forge_setup.subprocess.run", return_value=subprocess.CompletedProcess([], 128, b"", b"secret")) as run:
            with self.assertRaises(ForgeSetupError):
                _anonymous_git_read("http://127.0.0.1:3000/finance/workbook-tools.git")
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["docker", "compose", "exec", "-T"])
        self.assertIn("forge", command)
        self.assertIn("GIT_TERMINAL_PROMPT=0", command)

    def test_anonymous_git_requires_exact_main_ref(self) -> None:
        url = "http://127.0.0.1:3000/finance/workbook-tools.git"
        for output in (b"", b"a" * 40 + b"\trefs/heads/maintenance\n"):
            with self.subTest(output=output), patch(
                "forge_setup.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, b"")
            ):
                with self.assertRaisesRegex(ForgeSetupError, "baseline main branch"):
                    _anonymous_git_read(url)
        output = b"a" * 40 + b"\trefs/heads/maintenance\n" + b"b" * 40 + b"\trefs/heads/main\n"
        with patch("forge_setup.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, b"")):
            _anonymous_git_read(url)

    def test_cli_success_prints_only_repo_url_json(self) -> None:
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["forge_setup.py", "--state-dir", str(self.state)]), patch(
            "forge_setup.provision_forge", return_value={"repo_url": "http://forge:3000/finance/workbook-tools.git"}
        ), contextlib.redirect_stdout(stdout):
            main()
        self.assertEqual(json.loads(stdout.getvalue()), {"repo_url": "http://forge:3000/finance/workbook-tools.git"})
        self.assertEqual(stdout.getvalue().count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
