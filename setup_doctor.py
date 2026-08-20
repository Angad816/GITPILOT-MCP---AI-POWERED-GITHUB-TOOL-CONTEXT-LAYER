"""Beginner-friendly, read-only diagnostics for a local GitPilot setup.

Every check returns status/message/recovery/details and never prints or
stores a secret value. Run directly with `python setup_doctor.py`.
"""

import importlib
import os
import shutil
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from services.github_service import GitHubService, GitHubServiceError
from services.code_service import code_service as _code_service
from services.crypto import CryptoError, TokenCipher

MIN_PYTHON = (3, 10)
REQUIRED_MODULES = ["mcp", "httpx", "dotenv", "starlette", "uvicorn"]
PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass
class DiagnosticResult:
    id: str
    title: str
    status: str  # "pass" | "warn" | "fail"
    message: str
    recovery: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "message": self.message,
            "recovery": self.recovery,
            "details": self.details,
        }


def check_python_runtime(version_info: tuple | None = None) -> DiagnosticResult:
    version = version_info or sys.version_info
    ok = (version[0], version[1]) >= MIN_PYTHON
    return DiagnosticResult(
        id="python_runtime",
        title="Python runtime",
        status="pass" if ok else "fail",
        message=(
            f"Python {version[0]}.{version[1]} meets the {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ requirement."
            if ok
            else f"Python {version[0]}.{version[1]} is older than the required {MIN_PYTHON[0]}.{MIN_PYTHON[1]}."
        ),
        recovery=None if ok else f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer and recreate the virtual environment.",
        details={"version": f"{version[0]}.{version[1]}.{version[2] if len(version) > 2 else 0}"},
    )


def check_dependencies(import_module: Callable[[str], Any] = importlib.import_module) -> DiagnosticResult:
    missing = []
    for name in REQUIRED_MODULES:
        try:
            import_module(name)
        except ImportError:
            missing.append(name)
    ok = not missing
    return DiagnosticResult(
        id="dependencies",
        title="Required dependencies",
        status="pass" if ok else "fail",
        message="All required Python packages are importable." if ok else f"Missing packages: {', '.join(missing)}.",
        recovery=None if ok else "Run: pip install -r requirements.txt",
        details={"required": REQUIRED_MODULES, "missing": missing},
    )


def check_github_token_present(env: dict | None = None, stored_connection: dict[str, Any] | None = None) -> DiagnosticResult:
    source = env if env is not None else os.environ
    token = (source.get("GITHUB_TOKEN") or "").strip()
    source_name = "environment" if token else "encrypted dashboard vault" if stored_connection else None
    ok = source_name is not None
    return DiagnosticResult(
        id="github_token_present",
        title="GitHub connection configured",
        status="pass" if ok else "fail",
        message=f"GitHub access is configured through the {source_name}." if ok else "No GitHub access token is configured.",
        recovery=None if ok else "Connect GitHub in the dashboard wizard, or set GITHUB_TOKEN in .env.",
        details={"configured": ok, "source": source_name},
    )


def check_github_auth(service: Any = None) -> DiagnosticResult:
    token = (os.getenv("GITHUB_TOKEN") or "").strip()
    if service is None and not token:
        return DiagnosticResult(
            id="github_auth",
            title="GitHub authentication",
            status="fail",
            message="Cannot verify GitHub authentication because no token is configured.",
            recovery="Configure GITHUB_TOKEN first, then re-run the doctor.",
        )
    try:
        target = service or GitHubService()
        health = target.health()
        owner = health.get("owner")
        authenticated_as = health.get("authenticated_as")
        return DiagnosticResult(
            id="github_auth",
            title="GitHub authentication",
            status="pass",
            message=f"Authenticated to GitHub as {authenticated_as}.",
            recovery=None,
            details={"owner": owner, "authenticated_as": authenticated_as, "rate_limit": health.get("rate_limit")},
        )
    except GitHubServiceError as exc:
        return DiagnosticResult(
            id="github_auth",
            title="GitHub authentication",
            status="fail",
            message=f"GitHub authentication failed: {exc}",
            recovery="Check that GITHUB_TOKEN is valid, unexpired, and has not been revoked.",
            details={"http_status": exc.status},
        )
    except Exception as exc:  # network/DNS failures etc.
        return DiagnosticResult(
            id="github_auth",
            title="GitHub authentication",
            status="fail",
            message="Could not reach GitHub to verify authentication.",
            recovery="Check your network connection and GITHUB_API_URL, then try again.",
            details={"error_type": type(exc).__name__},
        )


def check_repository_access(service: Any = None) -> DiagnosticResult:
    token = (os.getenv("GITHUB_TOKEN") or "").strip()
    if service is None and not token:
        return DiagnosticResult(
            id="repository_access",
            title="Repository access",
            status="fail",
            message="Cannot check repository access because no token is configured.",
            recovery="Configure GITHUB_TOKEN first, then re-run the doctor.",
        )
    try:
        target = service or GitHubService()
        repos = target.list_repositories()
        count = len(repos)
        ok = count > 0
        return DiagnosticResult(
            id="repository_access",
            title="Repository access",
            status="pass" if ok else "warn",
            message=(
                f"Found {count} repositories accessible to this GitHub connection."
                if ok
                else "No accessible repositories were found for this GitHub connection."
            ),
            recovery=None if ok else "Grant the token access to a repository, or ask its owner or organization administrator to approve access.",
            details={"repository_count": count},
        )
    except GitHubServiceError as exc:
        return DiagnosticResult(
            id="repository_access",
            title="Repository access",
            status="fail",
            message=f"Could not list repositories: {exc}",
            recovery="Verify GITHUB_TOKEN scopes include repository access.",
            details={"http_status": exc.status},
        )
    except Exception as exc:
        return DiagnosticResult(
            id="repository_access",
            title="Repository access",
            status="fail",
            message="Could not reach GitHub to list repositories.",
            recovery="Check your network connection and try again.",
            details={"error_type": type(exc).__name__},
        )


def check_database_writable(database_path: Path | str | None = None) -> DiagnosticResult:
    path = Path(database_path or os.getenv("GITPILOT_MEMORY_DB") or (PROJECT_ROOT / "data" / "gitpilot.db")).resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / f".gitpilot_doctor_probe_{os.getpid()}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return DiagnosticResult(
            id="database_writable",
            title="Memory database directory",
            status="pass",
            message=f"{path.parent} is writable.",
            details={"path": str(path)},
        )
    except OSError as exc:
        return DiagnosticResult(
            id="database_writable",
            title="Memory database directory",
            status="fail",
            message=f"Cannot write to {path.parent}: {exc}",
            recovery="Check folder permissions or change GITPILOT_MEMORY_DB to a writable location.",
            details={"path": str(path)},
        )


def check_code_workspaces(service: Any = None) -> DiagnosticResult:
    target = service or _code_service
    try:
        workspaces = target.workspaces()
    except Exception as exc:
        return DiagnosticResult(
            id="code_workspaces",
            title="Code workspaces",
            status="fail",
            message="Could not read code workspace configuration.",
            recovery="Check GITPILOT_CODE_ROOTS syntax: name=path;name2=path2",
            details={"error_type": type(exc).__name__},
        )
    missing = [item for item in workspaces if not Path(item["path"]).is_dir()]
    ok = not missing
    return DiagnosticResult(
        id="code_workspaces",
        title="Code workspaces",
        status="pass" if ok else "warn",
        message=(
            f"{len(workspaces)} local workspace(s) are mapped and readable."
            if ok
            else f"{len(missing)} mapped workspace path(s) do not exist on disk."
        ),
        recovery=None if ok else "Fix the paths in GITPILOT_CODE_ROOTS or remove the stale mapping.",
        details={"workspaces": [item["name"] for item in workspaces], "missing": [item["name"] for item in missing]},
    )


def check_mcp_server_importable(import_module: Callable[[str], Any] = importlib.import_module) -> DiagnosticResult:
    try:
        import_module("server")
        return DiagnosticResult(
            id="mcp_server_import",
            title="MCP server readiness",
            status="pass",
            message="server.py imports successfully and MCP tools are registered.",
        )
    except Exception as exc:
        return DiagnosticResult(
            id="mcp_server_import",
            title="MCP server readiness",
            status="fail",
            message=f"server.py failed to import: {exc}",
            recovery="Run `python -m py_compile server.py` to see the full traceback.",
            details={"error_type": type(exc).__name__},
        )


def check_dashboard_port(host: str | None = None, port: int | None = None) -> DiagnosticResult:
    resolved_host = host or os.getenv("GITPILOT_HOST", "127.0.0.1")
    resolved_port = int(port if port is not None else os.getenv("GITPILOT_PORT", "8765"))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        in_use = sock.connect_ex((resolved_host, resolved_port)) == 0
    return DiagnosticResult(
        id="dashboard_port",
        title="Dashboard port availability",
        status="warn" if in_use else "pass",
        message=(
            f"Port {resolved_port} on {resolved_host} is already in use (this may be GitPilot's dashboard already running)."
            if in_use
            else f"Port {resolved_port} on {resolved_host} is free for the dashboard."
        ),
        recovery="Stop the process using that port, or set GITPILOT_PORT to a free port." if in_use else None,
        details={"host": resolved_host, "port": resolved_port},
    )


def check_hosted_auth_configuration(env: dict | None = None) -> DiagnosticResult:
    source = env if env is not None else os.environ
    client_id = bool((source.get("GITHUB_OAUTH_CLIENT_ID") or "").strip())
    client_secret = bool((source.get("GITHUB_OAUTH_CLIENT_SECRET") or "").strip())
    master_key = (source.get("GITPILOT_MASTER_KEY") or "").strip()
    present = {"GITHUB_OAUTH_CLIENT_ID": client_id, "GITHUB_OAUTH_CLIENT_SECRET": client_secret, "GITPILOT_MASTER_KEY": bool(master_key)}

    if not any(present.values()):
        return DiagnosticResult(
            id="hosted_auth",
            title="Hosted multi-tenant login",
            status="pass",
            message="Not configured — GitPilot is running in local, single-owner mode.",
            details={"hosted_mode": False},
        )

    missing = [name for name, is_present in present.items() if not is_present]
    if missing:
        return DiagnosticResult(
            id="hosted_auth",
            title="Hosted multi-tenant login",
            status="warn",
            message=f"Partially configured — missing {', '.join(missing)}. GitPilot stays in local, single-owner mode until all three are set.",
            recovery="Set all three hosted-login variables, or leave all three unset to stay in local mode.",
            details={"hosted_mode": False, "missing": missing},
        )

    try:
        TokenCipher(keys=[key.strip() for key in master_key.split(",") if key.strip()])
    except CryptoError:
        return DiagnosticResult(
            id="hosted_auth",
            title="Hosted multi-tenant login",
            status="fail",
            message="GITPILOT_MASTER_KEY is set but is not a valid Fernet key.",
            recovery='Generate a new key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"',
            details={"hosted_mode": True},
        )
    return DiagnosticResult(
        id="hosted_auth",
        title="Hosted multi-tenant login",
        status="pass",
        message="Hosted login is fully configured.",
        details={"hosted_mode": True},
    )


def check_github_app_configuration(env: dict | None = None) -> DiagnosticResult:
    source = env if env is not None else os.environ
    app_id = bool((source.get("GITHUB_APP_ID") or "").strip())
    private_key_path = (source.get("GITHUB_APP_PRIVATE_KEY_PATH") or "").strip()
    present = {"GITHUB_APP_ID": app_id, "GITHUB_APP_PRIVATE_KEY_PATH": bool(private_key_path)}

    if not any(present.values()):
        return DiagnosticResult(
            id="github_app",
            title="GitHub App connection",
            status="pass",
            message="Not configured — the dashboard's Connect Repository wizard uses your existing GitHub connection for GitHub access.",
            details={"configured": False},
        )

    missing = [name for name, is_present in present.items() if not is_present]
    if missing:
        return DiagnosticResult(
            id="github_app",
            title="GitHub App connection",
            status="warn",
            message=f"Partially configured — missing {', '.join(missing)}. The GitHub App install flow stays unavailable until both are set.",
            recovery="Set both GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH, or leave both unset. See docs/BRAIN.md.",
            details={"configured": False, "missing": missing},
        )

    if not Path(private_key_path).is_file():
        return DiagnosticResult(
            id="github_app",
            title="GitHub App connection",
            status="fail",
            message="GITHUB_APP_PRIVATE_KEY_PATH is set but the file does not exist.",
            recovery="Download the GitHub App's private key PEM and point GITHUB_APP_PRIVATE_KEY_PATH at it.",
            details={"configured": True},
        )
    return DiagnosticResult(
        id="github_app",
        title="GitHub App connection",
        status="pass",
        message="GitHub App connection is configured.",
        details={"configured": True},
    )


def check_claude_registration_guidance(which: Callable[[str], str | None] = shutil.which) -> DiagnosticResult:
    python_exe = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    server_path = PROJECT_ROOT / "server.py"
    command = f'claude mcp add gitpilot -- "{python_exe}" "{server_path}"'
    claude_found = which("claude") is not None
    return DiagnosticResult(
        id="claude_registration",
        title="Claude MCP registration",
        status="pass" if claude_found else "warn",
        message=(
            "The Claude CLI is on PATH. Register GitPilot with the command below."
            if claude_found
            else "The Claude CLI was not found on PATH. Install it, then register GitPilot with the command below."
        ),
        recovery=command,
        details={"claude_cli_found": claude_found},
    )


def run_diagnostics(*, github_service: Any = None, code_provider: Any = None) -> list[dict[str, Any]]:
    stored_connection = None
    resolved_github_service = github_service
    if resolved_github_service is None and not (os.getenv("GITHUB_TOKEN") or "").strip():
        try:
            from services.github_token_service import github_token_service

            stored_connection = github_token_service.get_connection(None)
            if stored_connection:
                resolved_github_service = github_token_service.build_github_service(None)
        except Exception:
            # The auth check below gives the user a safe recovery message without
            # exposing encrypted credential or database details.
            resolved_github_service = None
    checks = [
        check_python_runtime(),
        check_dependencies(),
        check_github_token_present(stored_connection=stored_connection),
        check_github_auth(resolved_github_service),
        check_repository_access(resolved_github_service),
        check_database_writable(),
        check_code_workspaces(code_provider),
        check_mcp_server_importable(),
        check_dashboard_port(),
        check_hosted_auth_configuration(),
        check_github_app_configuration(),
        check_claude_registration_guidance(),
    ]
    return [check.to_dict() for check in checks]


STATUS_LABEL = {"pass": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}


def main() -> int:
    print("GitPilot Setup Doctor")
    print("=" * 60)
    exit_code = 0
    for result in run_diagnostics():
        label = STATUS_LABEL.get(result["status"], "[ ?? ]")
        print(f"{label} {result['title']}")
        print(f"       {result['message']}")
        if result["recovery"]:
            print(f"       Next step: {result['recovery']}")
        if result["status"] == "fail":
            exit_code = 1
    print("=" * 60)
    print("One or more checks failed. Fix the items above and re-run." if exit_code else "All checks passed. GitPilot is ready.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
