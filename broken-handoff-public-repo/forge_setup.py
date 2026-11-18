"""Provision an isolated Gitea repository for the broken-handoff fixture.

Run from the project directory with the already-running forge container. API
requests execute inside that container; CLI output never includes secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote


ORG = "finance"
REPO = "workbook-tools"
PRODUCER = "producer"
ADMIN = "bootstrapadmin"
REPO_URL = "http://forge:3000/finance/workbook-tools.git"
FORGE_API_BASE = "http://127.0.0.1:3000"


class ForgeSetupError(RuntimeError):
    """A provisioning or public-read check failed."""


def _base_url(value: str) -> str:
    if value.rstrip("/") != FORGE_API_BASE:
        raise ValueError("Gitea API URL must be http://127.0.0.1:3000 inside the forge container")
    return FORGE_API_BASE


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ForgeSetupError("State path cannot be a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)


def _write_private(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ForgeSetupError("Credential file cannot be a symlink")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _create_admin(username: str, password: str, email: str) -> None:
    command = [
        "docker", "compose", "exec", "-T", "-u", "git", "forge",
        "gitea", "admin", "user", "create",
        "--username", username,
        "--password", password,
        "--email", email,
        "--admin", "--must-change-password=false",
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except OSError as exc:
        raise ForgeSetupError("Cannot run local docker compose admin bootstrap") from exc
    if result.returncode:
        raise ForgeSetupError("Local Gitea admin bootstrap failed")


def _curl_config(
    base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    auth: tuple[str, str] | None,
) -> bytes:
    # Curl reads all credentials and request bodies on stdin, not in argv or logs.
    lines = [
        f"url = {json.dumps(base + path)}",
        f"request = {json.dumps(method)}",
        'header = "Accept: application/json"',
    ]
    if payload is not None:
        lines.append('header = "Content-Type: application/json"')
        lines.append(f"data-binary = {json.dumps(json.dumps(payload))}")
    if auth is not None:
        lines.append(f"user = {json.dumps(auth[0] + ':' + auth[1])}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _api(
    base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    auth: tuple[str, str] | None = None,
    expected: tuple[int, ...] = (200,),
) -> dict[str, Any]:
    command = [
        "docker", "compose", "exec", "-T", "forge", "curl",
        "--silent", "--show-error", "--max-time", "15", "--noproxy", "*",
        "--config", "-", "--write-out", "\n%{http_code}",
    ]
    try:
        result = subprocess.run(
            command, input=_curl_config(base, method, path, payload, auth),
            capture_output=True, check=False, timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForgeSetupError(f"Gitea API {method} {path} could not complete inside forge") from exc
    if result.returncode:
        raise ForgeSetupError(f"Gitea API {method} {path} is unavailable inside forge")
    body, separator, code = result.stdout.rpartition(b"\n")
    if not separator or not code.isdigit():
        raise ForgeSetupError(f"Gitea API {method} {path} returned no HTTP status")
    if int(code) not in expected:
        raise ForgeSetupError(f"Gitea API {method} {path} returned HTTP {code.decode('ascii')}")
    if len(body) > 1024 * 1024:
        raise ForgeSetupError("Gitea API response is too large")
    if not body:
        return {}
    try:
        decoded = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ForgeSetupError(f"Gitea API {method} {path} did not return JSON") from None
    if not isinstance(decoded, dict):
        raise ForgeSetupError(f"Gitea API {method} {path} returned an unexpected JSON shape")
    return decoded


def _anonymous_git_read(url: str) -> None:
    try:
        result = subprocess.run(
            [
                "docker", "compose", "exec", "-T",
                "-e", "GIT_TERMINAL_PROMPT=0",
                "-e", "GIT_CONFIG_GLOBAL=/dev/null",
                "-e", "GIT_CONFIG_SYSTEM=/dev/null",
                "forge", "git", "-c", "credential.helper=", "-c", "core.askPass=/bin/false",
                "ls-remote", "--heads", url,
            ],
            capture_output=True, check=False, timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForgeSetupError("Anonymous Git read check could not complete inside forge") from exc
    if result.returncode:
        raise ForgeSetupError("Anonymous Git read failed inside forge")
    refs = [line.split(b"\t", 1)[1] for line in result.stdout.splitlines() if b"\t" in line]
    if b"refs/heads/main" not in refs:
        raise ForgeSetupError("Anonymous Git read did not show baseline main branch")


def provision_forge(
    state_dir: str | Path,
    *,
    base_url: str = FORGE_API_BASE,
    create_admin: Callable[[str, str, str], None] = _create_admin,
    api: Callable[..., dict[str, Any]] = _api,
    anonymous_git_read: Callable[[str], None] = _anonymous_git_read,
) -> dict[str, str]:
    """Create a public repo with a nonadmin writer and a repository-write PAT.

    The PAT's `write:repository` scope is resource-wide. The producer's exact
    write collaborator permission on the target repo limits its repo access.

    A failed run leaves the admin credential file for manual recovery and does
    not report success. It does not retry against existing identities or repos.
    """
    base = _base_url(base_url)
    state = Path(state_dir)
    evaluator = state / "evaluator"
    producer_private = state / "producer" / "private"
    admin_file = evaluator / "forge.json"
    credential_file = producer_private / "git-credentials"
    for path in (state, state / "producer", evaluator, producer_private):
        if path.is_symlink():
            raise ForgeSetupError("State path cannot be a symlink")
    if admin_file.exists() or admin_file.is_symlink() or credential_file.exists() or credential_file.is_symlink():
        raise ForgeSetupError("Forge credentials already exist; refusing to replace them")
    for path in (state, state / "producer", evaluator, producer_private):
        _private_dir(path)

    admin_password = secrets.token_urlsafe(36)
    producer_password = secrets.token_urlsafe(36)
    admin_email = "bootstrap@forge.invalid"
    producer_email = "producer@forge.invalid"
    create_admin(ADMIN, admin_password, admin_email)
    _write_private(
        admin_file,
        json.dumps(
            {"base_url": base, "username": ADMIN, "email": admin_email, "password": admin_password},
            indent=2,
        ) + "\n",
    )
    admin_auth = (ADMIN, admin_password)
    admin_user = api(base, "GET", "/api/v1/user", auth=admin_auth)
    if admin_user.get("login") != ADMIN or admin_user.get("is_admin") is not True:
        raise ForgeSetupError("Bootstrap account is not an authenticated admin")

    created = api(
        base, "POST", "/api/v1/admin/users",
        {"username": PRODUCER, "email": producer_email, "password": producer_password, "must_change_password": False},
        admin_auth, (201,),
    )
    if created.get("login") != PRODUCER or created.get("is_admin") is not False:
        raise ForgeSetupError("Producer account is not a confirmed nonadmin")
    producer_auth = (PRODUCER, producer_password)
    producer_user = api(base, "GET", "/api/v1/user", auth=producer_auth)
    if producer_user.get("login") != PRODUCER or producer_user.get("is_admin") is not False:
        raise ForgeSetupError("Producer login or nonadmin check failed")

    organization = api(base, "POST", "/api/v1/orgs", {"username": ORG, "visibility": "public"}, admin_auth, (201,))
    if organization.get("username") != ORG:
        raise ForgeSetupError("Organization creation was not confirmed")
    repo_path = f"/api/v1/repos/{ORG}/{REPO}"
    repo = api(
        base, "POST", f"/api/v1/orgs/{ORG}/repos",
        {"name": REPO, "private": False, "auto_init": True}, admin_auth, (201,),
    )
    if repo.get("name") != REPO or repo.get("private") is not False or (repo.get("owner") or {}).get("login") != ORG:
        raise ForgeSetupError("Public finance repository creation was not confirmed")

    api(
        base, "PUT", f"{repo_path}/collaborators/{PRODUCER}",
        {"permission": "write"}, admin_auth, (204,),
    )
    permission = api(base, "GET", f"{repo_path}/collaborators/{PRODUCER}/permission", auth=admin_auth)
    if permission.get("permission") != "write":
        raise ForgeSetupError("Producer does not have exactly write collaborator permission")

    token_result = api(
        base, "POST", f"/api/v1/users/{PRODUCER}/tokens",
        {"name": "workbook-writer", "scopes": ["write:repository"]}, producer_auth, (201,),
    )
    token = token_result.get("sha1")
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise ForgeSetupError("Gitea did not return a producer repository-write token")

    public_repo = api(base, "GET", repo_path)
    if (
        public_repo.get("name") != REPO
        or (public_repo.get("owner") or {}).get("login") != ORG
        or public_repo.get("private") is not False
    ):
        raise ForgeSetupError("Anonymous API read does not confirm a public repository")
    anonymous_git_read(f"{base}/{ORG}/{REPO}.git")

    _write_private(
        credential_file,
        f"http://{quote(PRODUCER, safe='')}:{quote(token, safe='')}@forge:3000\n",
    )
    return {"repo_url": REPO_URL}


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a local public Gitea repository")
    parser.add_argument("--state-dir", required=True, help="Private state directory")
    parser.add_argument("--base-url", default=FORGE_API_BASE, help="Internal forge-container URL (default: http://127.0.0.1:3000)")
    args = parser.parse_args()
    try:
        result = provision_forge(args.state_dir, base_url=args.base_url)
    except (ForgeSetupError, ValueError) as exc:
        print(f"Forge setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
