import base64
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from services.account_service import AccountServiceError, account_service
from services.ai_provider_service import AIProviderServiceError, ai_provider_service
from services.ai_providers import supported_providers
from services.ai_providers.errors import friendly_message, suggested_actions
from services.brain_service import BrainServiceError, brain_service
from services.crypto import CryptoError, TokenCipher
from services.git_isolation import GitIsolationError
from services.github_app_service import GitHubAppConfig, GitHubAppServiceError, github_app_configured, github_app_service
from services.github_service import GitHubConfig, GitHubService, GitHubServiceError
from services.github_token_service import GitHubTokenServiceError, github_token_service
from services.code_service import CodeServiceError, NullCodeProvider, code_service
from services.memory_service import MemoryServiceError, memory_service
from services.model_catalog import models_for
from services.model_router import model_router
from services.patch_validation import PatchValidationError
from services.password_reset_mailer import PasswordResetMailerError, password_reset_mailer
from services.workflow_service import WorkflowService, workflow_service


BASE_DIR = Path(__file__).resolve().parent
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gitpilot.dashboard")

# Used only when hosted OAuth login is not configured (today's single-owner,
# local-deployment behavior). Left in place, and its name unchanged, so a
# self-hosted single-owner deployment keeps working with zero new required
# configuration.
service = GitHubService()


class AuthRequiredError(RuntimeError):
    pass


def oauth_configured() -> bool:
    """Whether this deployment is running in hosted, multi-tenant mode.

    Evaluated live (not cached at import) so tests can toggle it per-case.
    Local single-owner deployments simply never set these three variables
    and keep using the module-level `service` singleton unchanged.
    """
    return bool(
        os.getenv("GITHUB_OAUTH_CLIENT_ID")
        and os.getenv("GITHUB_OAUTH_CLIENT_SECRET")
        and os.getenv("GITPILOT_MASTER_KEY")
    )


def account_login_required() -> bool:
    return (os.getenv("GITPILOT_REQUIRE_ACCOUNT_LOGIN") or "").strip().lower() in {"1", "true", "yes"}


def resolve_github_service_for(owner_id: str | None, repo_name: str | None) -> GitHubService:
    """Pure owner_id + repository -> GitHubService resolver (the concrete
    GithubServiceResolver from services/contracts.py). No Request coupling, so
    it's independently unit-testable across multiple owners/installations and
    is what WorkflowService calls per repository-scoped operation instead of
    being bound to one GitHub connection for its whole lifetime.

    When repo_name is given and a GitHub App is configured, prefer a token
    minted from an active installation that covers that repository -- the
    per-repo upgrade path described in docs/BRAIN.md. Falls through to the
    existing PAT/classic-OAuth resolution below when no matching active
    installation exists, so this stays purely additive: a deployment with no
    GitHub App configured, or no installation covering a given repo, behaves
    exactly as it always has.
    """
    hosted = oauth_configured()
    if repo_name and github_app_configured() and (not hosted or owner_id):
        installation = github_app_service.find_installation_for_repo(owner_id, repo_name)
        if installation is not None:
            return github_app_service.build_github_service(installation["installation_id"], installation["account_login"])
    token_service = github_token_service.build_github_service(owner_id)
    if token_service is not None:
        return token_service
    if not hosted and owner_id is None:
        return service
    if not owner_id:
        raise AuthRequiredError("Log in with GitHub to continue.")
    connection = memory_service.get_github_connection(owner_id)
    if connection is None:
        raise AuthRequiredError("Connect and validate your own GitHub access token to continue.")
    try:
        token = TokenCipher().decrypt(connection["encrypted_token"])
    except CryptoError as exc:
        raise AuthRequiredError(str(exc)) from exc
    return GitHubService(GitHubConfig(token=token, owner=connection["github_login"]))


def resolve_github_service(request: Request, repo_name: str | None = None) -> GitHubService:
    """Request-bound convenience wrapper around resolve_github_service_for --
    extracts the logged-in owner_id (hosted mode) from the session, then
    delegates. Direct dashboard routes use this; WorkflowService instead gets
    the pure resolver itself (see resolve_workflow_service) so it can call it
    fresh, per operation, with whichever repository that operation targets."""
    return resolve_github_service_for(resolve_owner_id(request), repo_name)


def resolve_owner_id(request: Request) -> str | None:
    """Resolve the tenant from an API key, native account, or GitHub OAuth."""
    authorization = request.headers.get("Authorization", "")
    if authorization:
        scheme, _, credential = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not credential:
            raise AuthRequiredError("Use Authorization: Bearer <GitPilot API key>.")
        try:
            return account_service.authenticate_api_key(credential)["id"]
        except AccountServiceError as exc:
            raise AuthRequiredError(str(exc)) from exc
    account_id = request.session.get("account_user_id")
    if account_id:
        return resolve_account_session(request)
    github_user_id = request.session.get("user_id")
    if github_user_id:
        return github_user_id
    if oauth_configured() or account_login_required():
        raise AuthRequiredError("Log in or create an account to continue.")
    return None


def resolve_workflow_service(request: Request) -> WorkflowService:
    """A per-request WorkflowService, given a github_resolver bound to this
    request's logged-in owner (hosted) or to local mode (owner_id=None) so
    every repository-scoped action inside it (prepare_issue_run, complete_run,
    generate_patch_proposal, publish_fix, ...) resolves its own GitHub App
    installation or PAT/OAuth connection per repository, rather than sharing
    one GitHub connection for every repo touched in the request.

    Hosted mode always builds a fresh instance (NullCodeProvider -- a hosted
    deployment's local filesystem is never exposed to logged-in users). Local
    mode reuses the shared singleton when no GitHub App is configured (zero
    behavior change from before this function existed); when a GitHub App is
    configured, local mode also gets a resolver-aware instance so its guided
    fixes can use an installation too. server.py's own workflow_service usage
    is untouched either way -- it stays on the plain PAT singleton, matching
    the existing "local code can't run from a hosted server" precedent for
    keeping the MCP process's own resolution simple and local-only.
    """
    if not oauth_configured():
        owner_id = resolve_owner_id(request)
        if owner_id is not None or github_app_configured() or github_token_service.get_connection(None) is not None:
            return WorkflowService(
                github=resolve_github_service(request),
                code=code_service,
                memory=memory_service,
                github_resolver=resolve_github_service_for,
            )
        return workflow_service
    return WorkflowService(
        github=resolve_github_service(request),
        code=NullCodeProvider(),
        memory=memory_service,
        github_resolver=resolve_github_service_for,
    )


SESSION_SECRET = os.getenv("GITPILOT_SESSION_SECRET") or os.getenv("GITPILOT_MASTER_KEY")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_urlsafe(32)
    if oauth_configured():
        logger.warning("GITPILOT_SESSION_SECRET is not set; using an ephemeral secret. Sessions will not survive a restart.")
SESSION_HTTPS_ONLY = (os.getenv("GITPILOT_HTTPS_ONLY") or "").strip().lower() in {"1", "true", "yes"}


@asynccontextmanager
async def lifespan(app):
    logger.info("GitPilot dashboard starting hosted_auth=%s", oauth_configured())
    # Initialize the vault before a user pastes a token. Local loopback installs
    # get a private persisted key; production remains environment-key only.
    TokenCipher()
    logger.info("GitPilot credential vault ready")
    cleanup_counts = account_service.cleanup_expired_records()
    logger.info(
        "GitPilot startup cleanup removed %s revoked API key row(s) and %s stale password-reset row(s)",
        cleanup_counts["api_keys_deleted"],
        cleanup_counts["password_resets_deleted"],
    )
    yield
    service.close()
    logger.info("GitPilot dashboard stopped")


async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api/"):
        # Every /api/ response reflects live, request-scoped state (GitHub
        # repos, brain/credential status, run state...). Without an explicit
        # no-store, a browser can serve a stale cached GET response (e.g. an
        # empty repository list fetched before a GitHub connection existed)
        # and a plain reload will never re-fetch it.
        response.headers["Cache-Control"] = "no-store"
    logger.info(
        "request id=%s method=%s path=%s status=%s duration_ms=%s",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


async def api_response(request, action):
    try:
        return JSONResponse(await action())
    except AuthRequiredError as exc:
        return JSONResponse({"error": True, "message": str(exc), "code": "login_required"}, status_code=401)
    except GitHubServiceError as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=exc.status)
    except GitHubAppServiceError as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=exc.status)
    except AccountServiceError as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=exc.status)
    except AIProviderServiceError as exc:
        if exc.code:
            return JSONResponse(
                {
                    "error": True,
                    "code": exc.code,
                    "message": friendly_message(exc.code),
                    "provider_detail": str(exc),
                    "actions": suggested_actions(exc.code),
                    "billing_url": exc.billing_url,
                    "credential_id": exc.credential_id,
                },
                status_code=exc.status,
            )
        return JSONResponse({"error": True, "message": str(exc)}, status_code=exc.status)
    except (BrainServiceError, PatchValidationError, GitIsolationError, GitHubTokenServiceError) as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=400)
    except (MemoryServiceError, CodeServiceError, ValueError, json.JSONDecodeError, TypeError) as exc:
        message = str(exc) if isinstance(exc, (MemoryServiceError, CodeServiceError)) else "Invalid request data."
        return JSONResponse({"error": True, "message": message}, status_code=400)


async def home(request):
    return FileResponse(BASE_DIR / "web" / "index.html")


async def session_info(request):
    result = {
        "hosted": oauth_configured(),
        "authenticated": False,
        "account_auth_available": True,
        "account_required": account_login_required(),
        "account_authenticated": False,
    }
    account_id = request.session.get("account_user_id")
    if account_id:
        try:
            account = account_service.get_account(account_id)
            current_version = account_service.get_session_version(account_id)
            cookie_version = request.session.get("account_session_version")
            if cookie_version is None or int(cookie_version) != current_version:
                request.session.clear()
                return JSONResponse(result)
            result["account"] = account
            result["account_authenticated"] = True
            result["github_oauth_available"] = oauth_configured()
            result["github_app_available"] = github_app_configured()
            result["github_connection"] = github_token_service.get_connection(account_id)
        except AccountServiceError:
            request.session.pop("account_user_id", None)
    user_id = request.session.get("user_id")
    if user_id:
        user = memory_service.get_user(user_id)
        result.update({"authenticated": True, "github_login": user["github_login"]})
    return JSONResponse(result)


async def account_register(request):
    payload = await request.json()

    def action():
        account = account_service.register(
            payload.get("display_name", ""),
            payload.get("email", ""),
            payload.get("password", ""),
            payload.get("github_profile", ""),
        )
        establish_account_session(request, account["id"])
        return {
            "account": account,
            "github_app_available": github_app_configured(),
            "github_oauth_available": oauth_configured(),
        }

    response = await api_response(request, lambda: _async(action))
    response.headers["Cache-Control"] = "no-store"
    return response


async def account_login(request):
    payload = await request.json()

    def action():
        account = account_service.authenticate(payload.get("email", ""), payload.get("password", ""))
        establish_account_session(request, account["id"])
        return {"account": account}

    return await api_response(request, lambda: _async(action))


async def account_logout(request):
    request.session.clear()
    return JSONResponse({"logged_out": True})


PASSWORD_RESET_RESPONSE = "If an active account exists for that email, GitPilot has prepared password recovery instructions."


def local_password_reset_allowed(request: Request) -> bool:
    environment = (os.getenv("GITPILOT_ENV") or "development").strip().casefold()
    configured = (os.getenv("GITPILOT_ALLOW_LOCAL_PASSWORD_RESET") or "true").strip().casefold()
    server_host = (os.getenv("GITPILOT_HOST") or "127.0.0.1").strip().casefold()
    client_host = (request.client.host if request.client else "").casefold()
    return (
        configured in {"1", "true", "yes"}
        and environment not in {"prod", "production", "staging"}
        and server_host in {"127.0.0.1", "localhost", "::1"}
        and client_host in {"127.0.0.1", "localhost", "::1", "testclient"}
    )


async def account_password_reset_request(request):
    payload = await request.json()

    def action():
        local_allowed = local_password_reset_allowed(request)
        smtp_ready = password_reset_mailer.configured
        if not smtp_ready and not local_allowed:
            # Nothing can deliver a reset anywhere -- this check runs before
            # any email-specific lookup and is identical for every request,
            # so surfacing it doesn't reveal whether the entered email is
            # registered. It only tells the caller the *system* is
            # misconfigured, not anything about the account.
            logger.error("Password reset requested but no delivery method (SMTP or local) is configured")
            raise AccountServiceError(
                "Password recovery is temporarily unavailable. Contact your administrator.", 503
            )
        # Local desktop delivery hands the token straight back to the same
        # loopback browser -- no email is sent, so the cooldown's purpose
        # (limiting outbound mail / abuse) doesn't apply there, and enforcing
        # it anyway would leave the local UI stuck on a dead-end screen.
        bypass_cooldown = local_allowed and not smtp_ready
        reset = account_service.request_password_reset(payload.get("email", ""), bypass_cooldown=bypass_cooldown)
        response = {"accepted": True, "message": PASSWORD_RESET_RESPONSE}
        if reset is None:
            return response
        reset_base_url = (os.getenv("GITPILOT_PUBLIC_URL") or str(request.base_url)).rstrip("/")
        reset_url = f"{reset_base_url}/?{urlencode({'reset_token': reset['token']})}"
        if smtp_ready:
            try:
                password_reset_mailer.send(
                    reset["email"],
                    reset_url,
                    account_service.PASSWORD_RESET_MINUTES,
                )
            except PasswordResetMailerError:
                logger.exception("Password reset email delivery failed")
            return response
        response.update({"delivery": "local", "reset_token": reset["token"]})
        return response

    result = await api_response(request, lambda: _async(action))
    result.headers["Cache-Control"] = "no-store"
    return result


async def account_password_reset_complete(request):
    payload = await request.json()

    def action():
        account_service.reset_password(payload.get("token", ""), payload.get("password", ""))
        request.session.clear()
        return {"reset": True, "message": "Password changed. Log in with your new password."}

    response = await api_response(request, lambda: _async(action))
    response.headers["Cache-Control"] = "no-store"
    return response


async def account_repository(request):
    payload = await request.json()

    def action():
        account_id = resolve_account_session(request)
        account = account_service.set_requested_repository(account_id, payload.get("repository", ""))
        github = github_token_service.build_github_service(account_id)
        if github is None:
            return account
        try:
            result = github.check_repository_access(account["requested_repository"])
            return account_service.update_repository_access(account_id, result)
        finally:
            github.close()

    return await api_response(
        request,
        lambda: _async(action),
    )


def establish_account_session(request: Request, account_id: str) -> None:
    request.session.clear()
    request.session["account_user_id"] = account_id
    request.session["account_session_version"] = account_service.get_session_version(account_id)


def resolve_account_session(request: Request) -> str:
    account_id = request.session.get("account_user_id")
    if not account_id:
        raise AuthRequiredError("Log in to manage your GitPilot API keys.")
    account_service.get_account(account_id)
    current_version = account_service.get_session_version(account_id)
    cookie_version = request.session.get("account_session_version")
    if cookie_version is None or int(cookie_version) != current_version:
        request.session.clear()
        raise AuthRequiredError("Your session expired after a security change. Log in again.")
    return account_id


async def account_api_keys(request):
    def action():
        owner_id = resolve_account_session(request)
        if request.method == "GET":
            return account_service.list_api_keys(owner_id)
        return None

    if request.method == "GET":
        return await api_response(request, lambda: _async(action))
    payload = await request.json()
    return await api_response(
        request,
        lambda: _async(account_service.issue_api_key, resolve_account_session(request), payload.get("label", "")),
    )


async def account_api_key_delete(request):
    return await api_response(
        request,
        lambda: _async(account_service.delete_api_key, resolve_account_session(request), request.path_params["key_id"]),
    )


async def login(request):
    if not oauth_configured():
        return JSONResponse({"error": True, "message": "Hosted login is not configured on this deployment."}, status_code=404)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    request.session["oauth_state"] = state
    request.session["oauth_code_verifier"] = verifier
    params = {
        "client_id": os.getenv("GITHUB_OAUTH_CLIENT_ID"),
        "redirect_uri": str(request.url_for("auth_callback")),
        "scope": "repo",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{urlencode(params)}")


async def auth_callback(request):
    if not oauth_configured():
        return JSONResponse({"error": True, "message": "Hosted login is not configured on this deployment."}, status_code=404)
    expected_state = request.session.pop("oauth_state", None)
    code_verifier = request.session.pop("oauth_code_verifier", None)
    provided_state = request.query_params.get("state")
    code = request.query_params.get("code")
    if not code or not expected_state or not provided_state or not secrets.compare_digest(expected_state, provided_state):
        return JSONResponse({"error": True, "message": "Login could not be verified. Please try logging in again."}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": os.getenv("GITHUB_OAUTH_CLIENT_ID"),
                    "client_secret": os.getenv("GITHUB_OAUTH_CLIENT_SECRET"),
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": str(request.url_for("auth_callback")),
                },
                headers={"Accept": "application/json"},
            )
            token_response.raise_for_status()
            token_payload = token_response.json()
            access_token = token_payload.get("access_token")
            if not access_token:
                message = token_payload.get("error_description", "GitHub did not return an access token.")
                return JSONResponse({"error": True, "message": message}, status_code=400)
            profile_response = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            )
            profile_response.raise_for_status()
            profile = profile_response.json()
    except httpx.HTTPError:
        return JSONResponse({"error": True, "message": "Could not complete GitHub login. Please try again."}, status_code=502)

    account_id = request.session.get("account_user_id")
    if account_id:
        try:
            github_token_service.save_oauth_connection(account_id, access_token, profile["login"])
            account_service.confirm_github_profile(account_id, profile["login"])
        except (AccountServiceError, GitHubTokenServiceError, GitHubServiceError) as exc:
            return JSONResponse({"error": True, "message": str(exc)}, status_code=500)
        establish_account_session(request, account_id)
        return RedirectResponse("/?github=connected")

    user = memory_service.upsert_user(profile["login"], profile["id"])
    try:
        cipher = TokenCipher()
    except CryptoError as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=500)
    memory_service.save_github_connection(user["id"], cipher.encrypt(access_token), profile["login"], token_payload.get("scope", ""))
    request.session["user_id"] = user["id"]
    return RedirectResponse("/")


async def logout(request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


async def disconnect_github(request):
    account_id = request.session.get("account_user_id")
    if account_id:
        github_token_service.delete_connection(account_id)
        account = account_service.get_account(account_id)
        if account.get("requested_repository"):
            account_service.set_requested_repository(account_id, account["requested_repository"])
        return JSONResponse({"disconnected": True})
    user_id = request.session.get("user_id")
    if not user_id:
        return JSONResponse({"error": True, "message": "Not logged in."}, status_code=401)
    try:
        connection = memory_service.get_github_connection(user_id)
        if connection is not None:
            token = TokenCipher().decrypt(connection["encrypted_token"])
            client_id = os.getenv("GITHUB_OAUTH_CLIENT_ID")
            client_secret = os.getenv("GITHUB_OAUTH_CLIENT_SECRET")
            async with httpx.AsyncClient(timeout=15, auth=(client_id, client_secret)) as client:
                await client.request(
                    "DELETE",
                    f"https://api.github.com/applications/{client_id}/grant",
                    json={"access_token": token},
                )
    except (CryptoError, httpx.HTTPError):
        pass  # best-effort revoke with GitHub; always clear local state below
    memory_service.delete_github_connection(user_id)
    request.session.clear()
    return JSONResponse({"disconnected": True})


async def health(request):
    return await api_response(request, lambda: _async(resolve_github_service(request).health))


async def repositories(request):
    return await api_response(request, lambda: _async(resolve_github_service(request).list_repositories))


async def github_connection_status(request):
    def action():
        owner_id = resolve_owner_id(request)
        stored = github_token_service.get_connection(owner_id)
        if stored is not None:
            return {"connected": True, "mode": "personal_access_token", **stored}
        if not oauth_configured() and owner_id is None and service.config.token:
            try:
                health_result = service.health()
                repositories_result = service.list_repositories()
                return {
                    "connected": True,
                    "mode": "environment",
                    "github_login": health_result.get("authenticated_as"),
                    "repository_count": len(repositories_result),
                    "label": ".env GITHUB_TOKEN",
                }
            except GitHubServiceError as exc:
                return {
                    "connected": False,
                    "mode": "environment",
                    "configured": True,
                    "message": "The existing .env GitHub token could not be validated. Paste a current token below to replace this connection.",
                }
        if oauth_configured() and owner_id:
            connection = memory_service.get_github_connection(owner_id)
            if connection is not None:
                return {
                    "connected": True,
                    "mode": "oauth",
                    "github_login": connection["github_login"],
                    "label": "GitHub login",
                }
        return {"connected": False, "mode": "none"}

    return await api_response(request, lambda: _async(action))


async def github_connection_create(request):
    payload = await request.json()

    def action():
        owner_id = resolve_owner_id(request)
        connection = github_token_service.save_connection(
            owner_id,
            payload.get("label", "GitHub access token"),
            payload.get("token", ""),
        )
        if owner_id and request.session.get("account_user_id") == owner_id:
            account_service.confirm_github_profile(owner_id, connection["github_login"])
        return connection

    return await api_response(request, lambda: _async(action))


async def github_connection_delete(request):
    def action():
        github_token_service.delete_connection(resolve_owner_id(request))
        return {"disconnected": True}

    return await api_response(request, lambda: _async(action))


async def github_connection_check_repository(request):
    payload = await request.json()

    def action():
        repository = (payload.get("repository") or "").strip()
        if not repository:
            raise GitHubTokenServiceError("Enter a repository as owner/repository.")
        return resolve_github_service(request).check_repository_access(repository)

    return await api_response(request, lambda: _async(action))


async def github_repository_invitations(request):
    return await api_response(
        request,
        lambda: _async(resolve_github_service(request).list_repository_invitations),
    )


async def github_repository_invitation_accept(request):
    return await api_response(
        request,
        lambda: _async(
            resolve_github_service(request).accept_repository_invitation,
            int(request.path_params["invitation_id"]),
        ),
    )


async def issues(request):
    state = request.query_params.get("state", "open")
    repo = request.path_params["repo"]
    return await api_response(request, lambda: _async(resolve_github_service(request, repo).get_issues, repo, state))


async def create(request):
    payload = await request.json()
    repo = request.path_params["repo"]
    return await api_response(
        request,
        lambda: _async(
            resolve_github_service(request, repo).create_issue,
            repo,
            payload.get("title"),
            payload.get("body", ""),
        ),
    )


async def close(request):
    repo = request.path_params["repo"]
    return await api_response(
        request,
        lambda: _async(resolve_github_service(request, repo).close_issue, repo, int(request.path_params["number"])),
    )


async def reopen(request):
    repo = request.path_params["repo"]
    return await api_response(
        request,
        lambda: _async(resolve_github_service(request, repo).reopen_issue, repo, int(request.path_params["number"])),
    )


async def update(request):
    payload = await request.json()
    repo = request.path_params["repo"]
    return await api_response(
        request,
        lambda: _async(
            resolve_github_service(request, repo).update_issue,
            repo,
            int(request.path_params["number"]),
            payload.get("title"),
            payload.get("body"),
        ),
    )


async def labels(request):
    repo = request.path_params["repo"]
    return await api_response(request, lambda: _async(resolve_github_service(request, repo).list_labels, repo))


async def update_labels(request):
    payload = await request.json()
    repo = request.path_params["repo"]
    return await api_response(
        request,
        lambda: _async(
            resolve_github_service(request, repo).set_issue_labels,
            repo,
            int(request.path_params["number"]),
            payload.get("labels", []),
        ),
    )


async def comments(request):
    repo = request.path_params["repo"]
    return await api_response(
        request,
        lambda: _async(resolve_github_service(request, repo).get_issue_comments, repo, int(request.path_params["number"])),
    )


async def add_comment(request):
    payload = await request.json()
    repo = request.path_params["repo"]
    return await api_response(
        request,
        lambda: _async(
            resolve_github_service(request, repo).add_issue_comment,
            repo,
            int(request.path_params["number"]),
            payload.get("body"),
        ),
    )


async def analyze_issue(request):
    repo = request.path_params["repo"]
    number = int(request.path_params["number"])
    try:
        github = resolve_github_service(request, repo)
    except AuthRequiredError as exc:
        return JSONResponse({"error": True, "message": str(exc), "code": "login_required"}, status_code=401)
    code_provider = code_service if not oauth_configured() else NullCodeProvider()

    async def analyze():
        issue = await _async(github.get_issue, repo, number)
        issue_comments = await _async(github.get_issue_comments, repo, number)
        query = "\n".join(
            [issue["title"], issue.get("body", ""), " ".join(issue.get("labels", [])), "\n".join(comment.get("body", "") for comment in issue_comments[-5:])]
        )
        context = await _async(code_provider.search, repo, query, 8, 12000)
        context["issue"] = issue
        context["verification_commands"] = code_provider.verification_commands(repo)
        context["model_plan"] = model_router.recommend(query, context)
        return context

    try:
        return JSONResponse(await analyze())
    except (GitHubServiceError, CodeServiceError) as exc:
        status = exc.status if isinstance(exc, GitHubServiceError) else 400
        return JSONResponse({"error": True, "message": str(exc)}, status_code=status)


async def repository_code_file(request):
    """Read an approved source file in bounded pages for the inspection dialog."""
    query = request.query_params
    path = query.get("path", "")

    def action():
        resolve_owner_id(request)
        provider = code_service if not oauth_configured() else NullCodeProvider()
        return provider.read_file(
            request.path_params["repo"],
            path,
            int(query.get("start_line", 1)),
            int(query.get("end_line", 500)),
        )

    return await api_response(request, lambda: _async(action))


async def platform_catalog(request):
    return await api_response(request, lambda: _async(resolve_workflow_service(request).repository_catalog))


async def repository_memory(request):
    query = request.query_params.get("query", "")

    def action():
        owner_id = resolve_owner_id(request)
        return memory_service.recall(request.path_params["repo"], query, 20, 16000, owner_id=owner_id)

    return await api_response(request, lambda: _async(action))


async def remember_repository(request):
    payload = await request.json()

    def action():
        owner_id = resolve_owner_id(request)
        return memory_service.remember(
            request.path_params["repo"], payload.get("kind", "note"), payload.get("title", ""), payload.get("content", ""), payload.get("tags", []), owner_id=owner_id
        )

    return await api_response(request, lambda: _async(action))


async def fix_runs(request):
    def action():
        owner_id = resolve_owner_id(request)
        return memory_service.list_runs(request.query_params.get("repository"), 50, owner_id=owner_id)

    return await api_response(request, lambda: _async(action))


async def start_issue_run(request):
    def action():
        owner_id = resolve_owner_id(request)
        return resolve_workflow_service(request).prepare_issue_run(request.path_params["repo"], int(request.path_params["number"]), 8, 12000, owner_id=owner_id)

    return await api_response(request, lambda: _async(action))


async def run_recovery(request):
    try:
        def action():
            owner_id = resolve_owner_id(request)
            return resolve_workflow_service(request).run_recovery(request.path_params["run_id"], owner_id=owner_id)

        return JSONResponse(await _async(action))
    except AuthRequiredError as exc:
        return JSONResponse({"error": True, "message": str(exc), "code": "login_required"}, status_code=401)
    except MemoryServiceError as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=404)


async def _optional_json_body(request) -> dict:
    """Like request.json(), but an empty body (the common case for these two
    routes, which usually take no input) returns {} instead of raising --
    only when a human explicitly retries with a different connection does a
    body get sent at all."""
    body = await request.body()
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


async def run_agent_diagnose(request):
    payload = await _optional_json_body(request)

    def action():
        owner_id = resolve_owner_id(request)
        return resolve_workflow_service(request).agent_diagnose(
            request.path_params["run_id"],
            owner_id=owner_id,
            override_credential_id=payload.get("credential_id"),
            override_model=payload.get("model"),
        )

    return await api_response(request, lambda: _async(action))


async def run_generate_proposal(request):
    payload = await _optional_json_body(request)

    def action():
        owner_id = resolve_owner_id(request)
        return resolve_workflow_service(request).generate_patch_proposal(
            request.path_params["run_id"],
            owner_id=owner_id,
            override_credential_id=payload.get("credential_id"),
            override_model=payload.get("model"),
        )

    return await api_response(request, lambda: _async(action))


async def run_auto_fix(request):
    payload = await _optional_json_body(request)

    def action():
        owner_id = resolve_owner_id(request)
        result = resolve_workflow_service(request).auto_fix_run(
            request.path_params["run_id"],
            owner_id=owner_id,
            max_attempts=payload.get("max_attempts", 3),
        )
        # auto_fix_run stays a 200 response even when it stops on a provider
        # failure (it's a normal stopping condition, not a request error) --
        # so the taxonomy's friendly copy/actions are added here rather than
        # in api_response's exception handling, matching the single-shot
        # AIProviderServiceError -> {message, actions} mapping below.
        provider_error = result.get("provider_error")
        if provider_error:
            provider_error["message"] = friendly_message(provider_error["code"])
            provider_error["actions"] = suggested_actions(provider_error["code"])
        return result

    return await api_response(request, lambda: _async(action))


async def run_manual_edit(request):
    """The manual no-AI fix path's edit step: makes services.workflow_service's
    existing edit_for_run (already used by the local MCP tool surface, see
    server.py) reachable from the dashboard too, so 'Fix manually -- no AI'
    is a complete path and not just a label. No AI provider, model, or
    credential is touched anywhere in this handler."""
    payload = await request.json()

    def action():
        owner_id = resolve_owner_id(request)
        return resolve_workflow_service(request).edit_for_run(
            request.path_params["run_id"],
            payload.get("path", ""),
            payload.get("old_text", ""),
            payload.get("new_text", ""),
            payload.get("reason", ""),
            owner_id=owner_id,
        )

    return await api_response(request, lambda: _async(action))


async def run_subscription_handoff(request):
    def action():
        owner_id = resolve_owner_id(request)
        return resolve_workflow_service(request).build_subscription_handoff(
            request.path_params["run_id"], owner_id=owner_id
        )

    return await api_response(request, lambda: _async(action))


async def run_import_subscription_proposal(request):
    payload = await request.json()

    def action():
        owner_id = resolve_owner_id(request)
        return resolve_workflow_service(request).import_subscription_proposal(
            request.path_params["run_id"], payload.get("proposal"), owner_id=owner_id
        )

    return await api_response(request, lambda: _async(action))


async def run_apply_proposal(request):
    payload = await request.json()

    def action():
        owner_id = resolve_owner_id(request)
        return resolve_workflow_service(request).approve_and_apply_patch(
            request.path_params["run_id"], owner_id=owner_id, proposal_hash=payload.get("proposal_hash")
        )

    return await api_response(request, lambda: _async(action))


async def run_verify(request):
    def action():
        owner_id = resolve_owner_id(request)
        return resolve_workflow_service(request).verify_run(request.path_params["run_id"], owner_id=owner_id)

    return await api_response(request, lambda: _async(action))


async def run_publish(request):
    payload = await request.json() if await request.body() else {}

    def action():
        owner_id = resolve_owner_id(request)
        return resolve_workflow_service(request).publish_fix(
            request.path_params["run_id"],
            owner_id=owner_id,
            close_issue=bool(payload.get("close_issue", False)),
            base_branch=payload.get("base_branch", "main"),
        )

    return await api_response(request, lambda: _async(action))


async def _async(function, *args):
    import anyio

    return await anyio.to_thread.run_sync(function, *args)


# --- GitHub App (repository access) ---------------------------------------------------------
# Additive to the existing PAT/classic-OAuth modes above: this only covers connecting and
# listing GitHub App installations. Issue/comment/label routes above continue to resolve
# GitHub access via resolve_github_service() (PAT or classic OAuth) unchanged -- wiring
# installation tokens into that per-request resolution is a follow-up once a real GitHub App
# is registered and per-repo installation routing can be validated end to end.


async def github_app_status(request):
    def action():
        owner_id = resolve_owner_id(request)
        return {
            "configured": github_app_configured(),
            "installations": github_app_service.list_installations(owner_id),
        }

    return await api_response(request, lambda: _async(action))


async def github_app_install_url(request):
    if not github_app_configured():
        return JSONResponse({"error": True, "message": "GitHub App is not configured on this deployment."}, status_code=404)
    config = GitHubAppConfig.from_env()
    state = secrets.token_urlsafe(32)
    request.session["github_app_state"] = state
    install_url = f"https://github.com/apps/{config.slug}/installations/new?state={state}"
    return JSONResponse({"install_url": install_url})


async def github_app_callback(request):
    if not github_app_configured():
        return JSONResponse({"error": True, "message": "GitHub App is not configured on this deployment."}, status_code=404)
    expected_state = request.session.pop("github_app_state", None)
    provided_state = request.query_params.get("state")
    installation_id = request.query_params.get("installation_id")
    setup_action = request.query_params.get("setup_action")
    if not installation_id or not expected_state or not provided_state or not secrets.compare_digest(expected_state, provided_state):
        return JSONResponse({"error": True, "message": "Installation could not be verified. Please try connecting again."}, status_code=400)

    def action():
        owner_id = resolve_owner_id(request)
        metadata = github_app_service.fetch_installation_metadata(int(installation_id))
        # GitHub itself renders "Request" instead of "Install" when the signed-in user
        # isn't authorized to install on the target org -- reading setup_action back is
        # the entire external-owner-approval mechanism. GitPilot never simulates approval.
        status = "pending_approval" if setup_action == "request" else "active"
        installation = github_app_service.save_installation(
            int(installation_id), metadata["account_login"], metadata["account_type"], owner_id,
            status, metadata["repository_selection"], metadata["permissions"],
        )
        account_id = request.session.get("account_user_id")
        if not account_id:
            return installation, "connected"
        if status == "pending_approval":
            account_service.update_repository_access(
                account_id,
                {
                    "accessible": False,
                    "message": "GitHub sent an installation request to the repository owner or organization administrator for approval.",
                },
            )
            return installation, "owner_approval_required"
        github_app_service.sync_installation_repositories(int(installation_id))
        if metadata["account_type"] == "User":
            account_service.confirm_github_profile(account_id, metadata["account_login"])
        return installation, "connected"

    try:
        _, outcome = await _async(action)
    except AuthRequiredError as exc:
        return JSONResponse({"error": True, "message": str(exc), "code": "login_required"}, status_code=401)
    except GitHubAppServiceError as exc:
        return JSONResponse({"error": True, "message": str(exc)}, status_code=exc.status)
    return RedirectResponse(f"/?github={outcome}")


async def github_app_webhook(request):
    payload = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not github_app_service.verify_webhook_signature(payload, signature):
        return JSONResponse({"error": True, "message": "Invalid webhook signature."}, status_code=401)
    event_type = request.headers.get("X-GitHub-Event", "")
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        return JSONResponse({"error": True, "message": "Invalid webhook payload."}, status_code=400)
    await _async(github_app_service.handle_webhook_event, event_type, body)
    return JSONResponse({"received": True})


async def github_app_installations(request):
    return await api_response(request, lambda: _async(lambda: github_app_service.list_installations(resolve_owner_id(request))))


async def github_app_forget_installation(request):
    def action():
        owner_id = resolve_owner_id(request)
        github_app_service.forget_installation(int(request.path_params["installation_id"]), owner_id)
        return {"forgotten": True}

    return await api_response(request, lambda: _async(action))


async def github_app_installation_repositories(request):
    def action():
        resolve_owner_id(request)  # enforces login when hosted; installation itself is the real boundary
        return github_app_service.sync_installation_repositories(int(request.path_params["installation_id"]))

    return await api_response(request, lambda: _async(action))


# --- AI provider credential vault -------------------------------------------------------------


async def ai_provider_list(request):
    def action():
        owner_id = resolve_owner_id(request)
        credentials = ai_provider_service.list_credentials(owner_id)
        for credential in credentials:
            brains = brain_service.brains_using_credential(credential["id"], owner_id=owner_id)
            credential["repositories"] = [brain["repository"] for brain in brains]
            credential["in_use"] = bool(brains)
        return credentials

    return await api_response(request, lambda: _async(action))


async def ai_provider_create(request):
    payload = await request.json()

    def action():
        owner_id = resolve_owner_id(request)
        return ai_provider_service.save_credential(
            owner_id,
            (payload.get("provider") or "").strip(),
            payload.get("label", ""),
            payload.get("api_key", ""),
            payload.get("base_url"),
            payload.get("default_model"),
        )

    return await api_response(request, lambda: _async(action))


async def ai_provider_validate(request):
    def action():
        owner_id = resolve_owner_id(request)
        return ai_provider_service.revalidate(request.path_params["credential_id"], owner_id)

    return await api_response(request, lambda: _async(action))


async def ai_provider_delete(request):
    def action():
        owner_id = resolve_owner_id(request)
        credential_id = request.path_params["credential_id"]
        ai_provider_service.get_credential(credential_id, owner_id)
        brains = brain_service.brains_using_credential(credential_id, owner_id=owner_id)
        disconnect = request.query_params.get("disconnect", "").casefold() == "true"
        if brains and not disconnect:
            repositories = ", ".join(brain["repository"] for brain in brains)
            raise AIProviderServiceError(
                f"This connection is used by {repositories}. Confirm disconnection to pause those Brain profiles before deleting the key."
            )
        disconnected = brain_service.disconnect_credential(credential_id, owner_id=owner_id) if brains else []
        ai_provider_service.delete_credential(credential_id, owner_id)
        return {
            "deleted": True,
            "disconnected_repositories": [brain["repository"] for brain in disconnected],
            "history_preserved": True,
        }

    return await api_response(request, lambda: _async(action))


async def ai_provider_models(request):
    provider = request.path_params["provider"]

    def action():
        if provider not in supported_providers():
            raise AIProviderServiceError(f"Unsupported AI provider: {provider}")
        return {"provider": provider, "models": models_for(provider)}

    return await api_response(request, lambda: _async(action))


async def ai_provider_discover_models(request):
    """Best-effort live model discovery for a saved connection -- primarily
    for local/self-hosted openai_compatible endpoints (Ollama, vLLM), whose
    real model list the static catalog can't know about. Never blocks the
    wizard: a failure or unsupported provider returns an empty, non-error
    result, so the static catalog and manual model entry remain available."""

    def action():
        owner_id = resolve_owner_id(request)
        return ai_provider_service.discover_models(request.path_params["credential_id"], owner_id=owner_id)

    return await api_response(request, lambda: _async(action))


# --- Repository Brain Profile ------------------------------------------------------------------


async def repository_brain_list(request):
    def action():
        return brain_service.list_brains(owner_id=resolve_owner_id(request))

    return await api_response(request, lambda: _async(action))


async def repository_brain_get(request):
    def action():
        owner_id = resolve_owner_id(request)
        return brain_service.get_brain(request.path_params["repo"], owner_id=owner_id)

    return await api_response(request, lambda: _async(action))


async def repository_brain_upsert(request):
    payload = await request.json()

    def action():
        owner_id = resolve_owner_id(request)
        credential_id = payload.get("credential_id", "")
        provider = payload.get("provider", "")
        credential = ai_provider_service.get_credential(credential_id, owner_id)
        if credential["provider"] != provider:
            raise BrainServiceError("The selected AI connection does not match this provider.")
        if credential["last_validation_status"] != "valid":
            raise BrainServiceError("Validate the selected AI connection before assigning it to a repository.")
        return brain_service.upsert_brain(
            owner_id,
            request.path_params["repo"],
            credential_id,
            provider,
            payload.get("model", ""),
            tier=payload.get("tier", "balanced"),
            max_output_tokens=payload.get("max_output_tokens", 1200),
            temperature=payload.get("temperature", 0.2),
            monthly_budget_usd=payload.get("monthly_budget_usd", 20.0),
            per_run_max_cost_usd=payload.get("per_run_max_cost_usd", 1.0),
        )

    return await api_response(request, lambda: _async(action))


async def repository_brain_activate(request):
    def action():
        owner_id = resolve_owner_id(request)
        return brain_service.activate(request.path_params["repo"], owner_id=owner_id)

    return await api_response(request, lambda: _async(action))


async def repository_brain_pause(request):
    def action():
        owner_id = resolve_owner_id(request)
        return brain_service.pause(request.path_params["repo"], owner_id=owner_id)

    return await api_response(request, lambda: _async(action))


async def repository_workflow_mode(request):
    payload = await request.json() if request.method == "PUT" else {}

    def action():
        owner_id = resolve_owner_id(request)
        if request.method == "PUT":
            return brain_service.set_workflow_mode(
                request.path_params["repo"], payload.get("mode", ""), owner_id=owner_id
            )
        return brain_service.get_workflow_mode(request.path_params["repo"], owner_id=owner_id)

    return await api_response(request, lambda: _async(action))


async def repository_brain_usage(request):
    def action():
        owner_id = resolve_owner_id(request)
        brain = brain_service.get_brain(request.path_params["repo"], owner_id=owner_id)
        if brain is None:
            raise BrainServiceError("No brain profile is configured for this repository yet.")
        return brain_service.usage_summary(brain["id"])

    return await api_response(request, lambda: _async(action))


async def repository_brain_estimate(request):
    payload = dict(request.query_params)

    def action():
        owner_id = resolve_owner_id(request)
        context = {"files": [], "characters": int(payload.get("characters", 0))}
        return resolve_workflow_service(request).estimate_with_budget(
            request.path_params["repo"], owner_id, payload.get("problem", ""), context
        )

    return await api_response(request, lambda: _async(action))


routes = [
    Route("/", home),
    Route("/api/health", health),
    Route("/api/session", session_info),
    Route("/api/account/register", account_register, methods=["POST"]),
    Route("/api/account/login", account_login, methods=["POST"]),
    Route("/api/account/logout", account_logout, methods=["POST"]),
    Route("/api/account/password-reset/request", account_password_reset_request, methods=["POST"]),
    Route("/api/account/password-reset/complete", account_password_reset_complete, methods=["POST"]),
    Route("/api/account/repository", account_repository, methods=["PATCH"]),
    Route("/api/account/api-keys", account_api_keys, methods=["GET", "POST"]),
    Route("/api/account/api-keys/{key_id:str}", account_api_key_delete, methods=["DELETE"]),
    Route("/auth/login", login),
    Route("/auth/callback", auth_callback, name="auth_callback"),
    Route("/auth/logout", logout, methods=["POST"]),
    Route("/auth/disconnect-github", disconnect_github, methods=["POST"]),
    Route("/api/repositories", repositories),
    Route("/api/github-connections/status", github_connection_status),
    Route("/api/github-connections", github_connection_create, methods=["POST"]),
    Route("/api/github-connections", github_connection_delete, methods=["DELETE"]),
    Route("/api/github-connections/check-repository", github_connection_check_repository, methods=["POST"]),
    Route("/api/github-connections/invitations", github_repository_invitations),
    Route("/api/github-connections/invitations/{invitation_id:int}/accept", github_repository_invitation_accept, methods=["POST"]),
    Route("/api/repositories/{repo:str}/issues", issues),
    Route("/api/repositories/{repo:str}/issues", create, methods=["POST"]),
    Route("/api/repositories/{repo:str}/issues/{number:int}/close", close, methods=["POST"]),
    Route("/api/repositories/{repo:str}/issues/{number:int}/reopen", reopen, methods=["POST"]),
    Route("/api/repositories/{repo:str}/issues/{number:int}", update, methods=["PATCH"]),
    Route("/api/repositories/{repo:str}/labels", labels),
    Route("/api/repositories/{repo:str}/issues/{number:int}/labels", update_labels, methods=["PUT"]),
    Route("/api/repositories/{repo:str}/issues/{number:int}/comments", comments),
    Route("/api/repositories/{repo:str}/issues/{number:int}/comments", add_comment, methods=["POST"]),
    Route("/api/repositories/{repo:str}/issues/{number:int}/code-context", analyze_issue),
    Route("/api/repositories/{repo:str}/code-file", repository_code_file),
    Route("/api/platform/repositories", platform_catalog),
    Route("/api/repositories/{repo:str}/memory", repository_memory),
    Route("/api/repositories/{repo:str}/memory", remember_repository, methods=["POST"]),
    Route("/api/runs", fix_runs),
    Route("/api/repositories/{repo:str}/issues/{number:int}/runs", start_issue_run, methods=["POST"]),
    Route("/api/runs/{run_id:str}/recovery", run_recovery),
    Route("/api/runs/{run_id:str}/diagnose", run_agent_diagnose, methods=["POST"]),
    Route("/api/runs/{run_id:str}/proposal", run_generate_proposal, methods=["POST"]),
    Route("/api/runs/{run_id:str}/auto-fix", run_auto_fix, methods=["POST"]),
    Route("/api/runs/{run_id:str}/edit", run_manual_edit, methods=["POST"]),
    Route("/api/runs/{run_id:str}/subscription-handoff", run_subscription_handoff, methods=["POST"]),
    Route("/api/runs/{run_id:str}/proposal/import", run_import_subscription_proposal, methods=["POST"]),
    Route("/api/runs/{run_id:str}/proposal/apply", run_apply_proposal, methods=["POST"]),
    Route("/api/runs/{run_id:str}/verify", run_verify, methods=["POST"]),
    Route("/api/runs/{run_id:str}/publish", run_publish, methods=["POST"]),
    Route("/api/github-app/status", github_app_status),
    Route("/api/github-app/install-url", github_app_install_url),
    Route("/auth/github-app/callback", github_app_callback, name="github_app_callback"),
    Route("/api/webhooks/github-app", github_app_webhook, methods=["POST"]),
    Route("/api/github-app/installations", github_app_installations),
    Route("/api/github-app/installations/{installation_id:int}", github_app_forget_installation, methods=["DELETE"]),
    Route("/api/github-app/installations/{installation_id:int}/repositories", github_app_installation_repositories),
    Route("/api/ai-providers", ai_provider_list),
    Route("/api/ai-providers", ai_provider_create, methods=["POST"]),
    Route("/api/ai-providers/{credential_id:str}/validate", ai_provider_validate, methods=["POST"]),
    Route("/api/ai-providers/{credential_id:str}", ai_provider_delete, methods=["DELETE"]),
    Route("/api/ai-providers/{provider:str}/models", ai_provider_models),
    Route("/api/ai-providers/{credential_id:str}/discover-models", ai_provider_discover_models),
    Route("/api/brains", repository_brain_list),
    Route("/api/repositories/{repo:str}/brain", repository_brain_get),
    Route("/api/repositories/{repo:str}/brain", repository_brain_upsert, methods=["PUT"]),
    Route("/api/repositories/{repo:str}/brain/activate", repository_brain_activate, methods=["POST"]),
    Route("/api/repositories/{repo:str}/brain/pause", repository_brain_pause, methods=["POST"]),
    Route("/api/repositories/{repo:str}/workflow-mode", repository_workflow_mode, methods=["GET", "PUT"]),
    Route("/api/repositories/{repo:str}/brain/usage", repository_brain_usage),
    Route("/api/repositories/{repo:str}/brain/estimate", repository_brain_estimate),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="gitpilot_session",
    same_site="lax",
    https_only=SESSION_HTTPS_ONLY,
)
app.add_middleware(BaseHTTPMiddleware, dispatch=request_context)
app.mount("/static", StaticFiles(directory=BASE_DIR / "web"), name="static")


def main():
    uvicorn.run(
        "dashboard:app",
        host=os.getenv("GITPILOT_HOST", "127.0.0.1"),
        port=int(os.getenv("GITPILOT_PORT", "8765")),
        reload=False,
    )


if __name__ == "__main__":
    main()
