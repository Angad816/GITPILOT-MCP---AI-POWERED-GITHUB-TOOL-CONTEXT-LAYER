from typing import Any

from mcp.server import MCPServer

from services.github_service import (
    add_issue_comment,
    close_issue,
    create_issue,
    get_issues,
    get_issue,
    get_issue_comments,
    list_labels,
    list_repositories,
    list_repositories_with_issue_counts,
    reopen_issue,
    set_issue_labels,
    update_issue,
    GitHubServiceError,
    _service,
)
from services.ai_provider_service import AIProviderServiceError
from services.brain_service import BrainServiceError
from services.code_service import CodeServiceError, code_service
from services.git_isolation import GitIsolationError
from services.memory_service import MemoryServiceError, memory_service
from services.patch_validation import PatchValidationError
from services.workflow_service import workflow_service


mcp = MCPServer("GitPilot MCP")


def _issue_fix_context(repo_name: str, issue_number: int, max_files: int = 8, max_chars: int = 12000) -> dict[str, Any]:
    issue = get_issue(repo_name, issue_number)
    if isinstance(issue, dict) and issue.get("error"):
        return issue
    comments = get_issue_comments(repo_name, issue_number)
    comment_text = "\n".join(comment.get("body", "") for comment in comments[-5:]) if isinstance(comments, list) else ""
    query = "\n".join([issue["title"], issue.get("body", ""), " ".join(issue.get("labels", [])), comment_text])
    try:
        context = code_service.search(repo_name, query, max_files=max_files, max_chars=max_chars)
        return {
            "issue": issue,
            "code_context": context,
            "verification_commands": code_service.verification_commands(repo_name),
            "workflow": [
                "Inspect the ranked snippets and read only files needed for the fix.",
                "Use code_replace_exact_text for minimal, reviewable edits.",
                "Run the suggested verification commands in the local workspace.",
                "Summarize changed files and verification before closing the GitHub issue.",
            ],
        }
    except CodeServiceError as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def github_connection_status() -> dict[str, Any]:
    """Check GitHub authentication and current API rate-limit capacity."""
    try:
        return _service.health()
    except Exception as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def code_list_workspaces() -> list[dict[str, str]]:
    """List approved local code workspaces available for issue diagnosis."""
    return code_service.workspaces()


@mcp.tool()
def code_index_repository(repo_name: str, force: bool = False) -> dict[str, Any]:
    """Index approved source files for token-efficient local retrieval."""
    try:
        return code_service.index_repository(repo_name, force)
    except CodeServiceError as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def github_prepare_issue_fix(repo_name: str, issue_number: int, max_files: int = 8, max_chars: int = 12000) -> dict[str, Any]:
    """Build a compact issue-to-code fix packet with ranked snippets and test hints."""
    return _issue_fix_context(repo_name, issue_number, max_files, max_chars)


@mcp.tool()
def code_search_repository(repo_name: str, query: str, max_files: int = 8, max_chars: int = 12000) -> dict[str, Any]:
    """Search an approved local repository and return only ranked code snippets."""
    try:
        return code_service.search(repo_name, query, max_files, max_chars)
    except CodeServiceError as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def code_read_file(repo_name: str, path: str, start_line: int = 1, end_line: int = 250) -> dict[str, Any]:
    """Read a bounded line range from an approved source file."""
    try:
        return code_service.read_file(repo_name, path, start_line, end_line)
    except CodeServiceError as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def code_replace_exact_text(repo_name: str, path: str, old_text: str, new_text: str) -> dict[str, Any]:
    """Safely replace one exact source block inside an approved workspace."""
    try:
        return code_service.replace_text(repo_name, path, old_text, new_text)
    except (CodeServiceError, OSError, UnicodeError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def code_get_verification_commands(repo_name: str) -> list[str] | dict[str, Any]:
    """Return allow-listed project test commands inferred from repository manifests."""
    try:
        return code_service.verification_commands(repo_name)
    except CodeServiceError as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def code_run_verification(repo_name: str, timeout_seconds: int = 180) -> dict[str, Any]:
    """Run only manifest-derived project checks and return bounded output."""
    try:
        return code_service.run_verification(repo_name, timeout_seconds)
    except CodeServiceError as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def platform_repository_catalog() -> list[dict[str, Any]] | dict[str, Any]:
    """List every connected GitHub repository and whether its local code is available."""
    try:
        return workflow_service.repository_catalog()
    except (GitHubServiceError, CodeServiceError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def memory_remember(repository: str, kind: str, title: str, content: str, tags: list[str] | None = None) -> dict[str, Any]:
    """Store durable repository knowledge for future AI sessions."""
    try:
        return memory_service.remember(repository, kind, title, content, tags)
    except MemoryServiceError as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def memory_recall(repository: str, query: str, limit: int = 8, max_chars: int = 8000) -> dict[str, Any]:
    """Recall compact, relevant project memory across conversations."""
    try:
        return memory_service.recall(repository, query, limit, max_chars)
    except MemoryServiceError as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_start_issue_fix(repo_name: str, issue_number: int, max_files: int = 8, max_chars: int = 12000) -> dict[str, Any]:
    """Start an auditable issue-fix run with code context and durable project memory."""
    try:
        return workflow_service.prepare_issue_run(repo_name, issue_number, max_files, max_chars)
    except (GitHubServiceError, CodeServiceError, MemoryServiceError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_read_file(run_id: str, path: str, start_line: int = 1, end_line: int = 250) -> dict[str, Any]:
    """Read a bounded file range and record the action in the fix run."""
    try:
        return workflow_service.read_for_run(run_id, path, start_line, end_line)
    except (CodeServiceError, MemoryServiceError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_edit_file(run_id: str, path: str, old_text: str, new_text: str, reason: str) -> dict[str, Any]:
    """Apply one exact edit and record its reason in durable run history."""
    try:
        return workflow_service.edit_for_run(run_id, path, old_text, new_text, reason)
    except (CodeServiceError, MemoryServiceError, OSError, UnicodeError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_verify_fix(run_id: str, timeout_seconds: int = 180) -> dict[str, Any]:
    """Run approved checks and transition the fix run based on evidence."""
    try:
        return workflow_service.verify_run(run_id, timeout_seconds)
    except (CodeServiceError, MemoryServiceError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_complete_issue_fix(run_id: str, resolution_summary: str, close_issue: bool = False) -> dict[str, Any]:
    """Persist the verified resolution, comment on GitHub, and optionally close the issue."""
    try:
        return workflow_service.complete_run(run_id, resolution_summary, close_issue)
    except (GitHubServiceError, MemoryServiceError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_get_fix_run(run_id: str) -> dict[str, Any]:
    """Get the complete auditable state and event history for a fix run."""
    try:
        return memory_service.get_run(run_id)
    except MemoryServiceError as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_run_recovery(run_id: str) -> dict[str, Any]:
    """Explain a fix run's current state, whether it can resume, and what already succeeded."""
    try:
        return workflow_service.run_recovery(run_id)
    except MemoryServiceError as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_agent_diagnose(run_id: str) -> dict[str, Any]:
    """Get a READ-ONLY AI diagnosis narrative from the repository's connected Brain Profile.

    Requires the repository to have an active Brain Profile (configured via the
    dashboard's "Connect repository and select your brain" wizard). Never edits,
    verifies, or closes anything -- use ai_edit_file / ai_verify_fix /
    ai_complete_issue_fix for those steps, each still requiring their own approval
    and verification."""
    try:
        return workflow_service.agent_diagnose(run_id)
    except (MemoryServiceError, BrainServiceError, AIProviderServiceError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_generate_patch_proposal(run_id: str) -> dict[str, Any]:
    """Get a structured, strictly-validated AI patch proposal for a fix run.

    READ-ONLY: nothing is written to disk. The model's response is validated
    (unknown fields, path traversal, out-of-evidence files, non-unique
    old_text, non-allow-listed verification commands are all rejected) before
    it is ever stored or returned. The returned proposal_hash is required by
    ai_approve_and_apply_patch as the explicit human-approval token -- review
    the proposal before approving it."""
    try:
        return workflow_service.generate_patch_proposal(run_id)
    except (MemoryServiceError, BrainServiceError, AIProviderServiceError, PatchValidationError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_approve_and_apply_patch(run_id: str, proposal_hash: str) -> dict[str, Any]:
    """Apply a previously generated patch proposal after explicit human approval.

    proposal_hash must exactly match the hash returned by
    ai_generate_patch_proposal for the proposal being approved -- this is the
    approval gate itself; a mismatched or stale hash is rejected. Edits are
    applied only on a dedicated isolated git branch, never on the
    repository's current branch, using the same exact-match replace as
    ai_edit_file. Run ai_verify_fix next."""
    try:
        return workflow_service.approve_and_apply_patch(run_id, proposal_hash=proposal_hash)
    except (CodeServiceError, MemoryServiceError, GitIsolationError, OSError, UnicodeError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def ai_publish_fix(run_id: str, close_issue: bool = False, base_branch: str = "main") -> dict[str, Any]:
    """Publish a verified fix: push its isolated branch, open a draft pull
    request, and optionally comment on / close the originating issue.

    Requires ai_verify_fix to have passed first (run status 'ready'). This is
    a separate, explicit publish-approval action on top of the edit approval
    in ai_approve_and_apply_patch -- publishing is idempotent, so retrying
    after a timeout or crash never creates duplicate branches, PRs, or
    comments."""
    try:
        return workflow_service.publish_fix(run_id, close_issue=close_issue, base_branch=base_branch)
    except (GitHubServiceError, MemoryServiceError, CodeServiceError, GitIsolationError) as exc:
        return {"error": True, "message": str(exc)}


@mcp.tool()
def github_list_repositories() -> list[dict[str, Any]] | dict[str, Any]:
    """List personal GitHub repositories accessible to the configured token."""
    return list_repositories()


@mcp.tool()
def github_list_repositories_with_issue_counts() -> list[dict[str, Any]] | dict[str, Any]:
    """List personal repositories with their open issue counts."""
    return list_repositories_with_issue_counts()


@mcp.tool()
def github_get_issues(repo_name: str, state: str = "open") -> list[dict[str, Any]] | dict[str, Any]:
    """Get open, closed, or all issues from a personal repository."""
    return get_issues(repo_name, state)


@mcp.tool()
def github_create_issue(repo_name: str, title: str, body: str = "") -> dict[str, Any]:
    """Create an issue in a personal repository."""
    return create_issue(repo_name, title, body)


@mcp.tool()
def github_close_issue(repo_name: str, issue_number: int) -> dict[str, Any]:
    """Close an issue after its work is completed."""
    return close_issue(repo_name, issue_number)


@mcp.tool()
def github_reopen_issue(repo_name: str, issue_number: int) -> dict[str, Any]:
    """Reopen a previously closed issue."""
    return reopen_issue(repo_name, issue_number)


@mcp.tool()
def github_update_issue(repo_name: str, issue_number: int, title: str | None = None, body: str | None = None) -> dict[str, Any]:
    """Update an issue title, body, or both."""
    return update_issue(repo_name, issue_number, title, body)


@mcp.tool()
def github_list_labels(repo_name: str) -> list[dict[str, str]] | dict[str, Any]:
    """List labels available in a personal repository."""
    return list_labels(repo_name)


@mcp.tool()
def github_set_issue_labels(repo_name: str, issue_number: int, labels: list[str]) -> dict[str, Any]:
    """Replace the labels assigned to an issue."""
    return set_issue_labels(repo_name, issue_number, labels)


@mcp.tool()
def github_get_issue_comments(repo_name: str, issue_number: int) -> list[dict[str, Any]] | dict[str, Any]:
    """Read comments from an issue."""
    return get_issue_comments(repo_name, issue_number)


@mcp.tool()
def github_add_issue_comment(repo_name: str, issue_number: int, body: str) -> dict[str, Any]:
    """Add a comment to an issue."""
    return add_issue_comment(repo_name, issue_number, body)


if __name__ == "__main__":
    mcp.run()
