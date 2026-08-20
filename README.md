# GitPilot MCP

GitPilot is a stateful AI engineering control plane for GitHub. It connects issues, approved local code, durable project memory, and auditable verification runs through MCP tools and a web dashboard.

## Capabilities

- List owned, collaborator, and organization repositories available to the connected GitHub identity.
- Create, update, label, comment on, close, and reopen issues.
- Recall repository knowledge across AI sessions.
- Retrieve ranked code snippets within fixed token budgets.
- Read bounded file ranges and apply exact-match edits.
- Run only manifest-derived verification commands.
- Require successful verification before completing an AI fix run.
- Keep an append-only history of context, reads, edits, checks, and completion.
- Connect a repository's own AI provider and generate a structured patch proposal, apply it only on explicit approval, verify it, and publish it as a draft pull request — never on a protected branch, never without a second explicit approval to publish.
- Optionally let GitPilot retry automatically after a failed verification (up to a bounded attempt count), carrying forward only the failure, not the whole repository — still stops for human review on a high-risk or low-confidence proposal, and never publishes on its own.
- Select a repository-scoped manual no-AI mode that blocks provider calls while retaining bounded evidence, exact edits, isolated branches, allow-listed verification, audit history, and draft-PR publishing — always one click away, even after a billing, quota, or credential failure on the AI path.

## Setup

```cmd
cd /d "C:\Projects - Building\GithubPilotMCP"
.venv\Scripts\activate.bat
pip install -r requirements.txt
copy .env.example .env
```

Start the dashboard and paste a GitHub personal access token into **Connect repository & brain**. On a loopback-only local install, GitPilot creates a private persisted vault key in `data/.gitpilot-master-key`, validates the token, encrypts it at rest, and never returns it to the browser. Production, staging, and network-exposed deployments must set `GITPILOT_MASTER_KEY` through secure secret storage. As a bootstrap alternative, set `GITHUB_TOKEN` in `.env`.

Prefer a fine-grained token with access only to intended repositories. Use Metadata read, Contents read/write, Issues read/write, and Pull requests read/write when you want the complete diagnose, patch, and draft-PR workflow. GitHub may require the repository owner or organization administrator to approve access; GitPilot cannot bypass that approval.

Map approved local repositories for code operations:

```env
GITPILOT_CODE_ROOTS=my-repo=C:\path\to\my-repo
```

Separate multiple mappings with semicolons. GitPilot lists all repositories available to the token, but it can read or edit code only in explicitly mapped workspaces.

## Setup Doctor

Before registering GitPilot with Claude, check that everything is configured correctly:

```cmd
python setup_doctor.py
```

Every check prints a plain-language status, an exact recovery step if something is wrong, and machine-readable details. It never prints a token value. The doctor recognizes both encrypted dashboard connections and `GITHUB_TOKEN`, then verifies Python, dependencies, GitHub authentication, repository access, SQLite, code workspaces, MCP readiness, the dashboard port, and Claude CLI registration readiness.

## Dashboard

```cmd
python dashboard.py
```

Open `http://127.0.0.1:8765`. Keep this interface on localhost or place it behind authenticated TLS.

The **Log in** control provides native email/password accounts without requiring Google. Account creation asks for the GitHub profile name, not a repository. Each account receives a unique customer ID. Passwords are salted and one-way hashed with scrypt; GitHub and AI credentials continue to be encrypted with `GITPILOT_MASTER_KEY` and are scoped to that customer ID. Customers can also create revocable `gp_live_...` GitPilot API keys from **Account & API access**. The plaintext API key is displayed once and only its SHA-256 hash is stored.

GitPilot never collects a GitHub username/password combination or a GitHub OTP. With a configured GitHub App, account creation continues to GitHub's installation screen so the customer chooses the exact repositories and permissions. With OAuth configured, GitPilot redirects to GitHub's authorization-code flow with state validation and PKCE; GitHub handles sign-in and two-factor authentication on `github.com`, then returns an authorization token. A validated personal access token remains the local fallback. The verified GitHub identity replaces any typed profile typo, and GitPilot lists only repositories that GitHub says the connection can access.

Repository selection happens after the connection is verified. The customer can run **Select repositories** again later after creating a repository, accepting an invitation, or expanding a token/App installation. Separate-owner and organization repositories remain subject to GitHub's approval rules.

Local guest preview remains available by default. For a deployed customer service, set `GITPILOT_REQUIRE_ACCOUNT_LOGIN=true`, configure a persistent `GITPILOT_SESSION_SECRET`, set `GITPILOT_HTTPS_ONLY=true`, and terminate traffic through HTTPS. Do not create Python or configuration files containing one customer's secrets; credential records belong in the encrypted, owner-scoped database vault.

For a reliable presentation without depending on venue connectivity or a live GitHub account, open `http://127.0.0.1:8765/?demo=1`. Demo mode is clearly labeled and uses simulated repository, issue, retrieval, memory, model, cost, GitHub connection, AI provider connection, and Brain Profile data. The normal URL always uses real configured services.

### Connect repository and select your brain

Click **Connect repository & brain** to open the setup wizard: paste and validate a GitHub token, select any accessible owned/collaborator/organization repository, choose DeepSeek, Anthropic, OpenAI-compatible/local, Google Gemini, or xAI Grok, connect the customer's own provider key, choose a model, set token and cost limits, and review everything before activating. This is BYOK (bring your own key): GitPilot never supplies a shared developer key, never falls back to the operator's key, and never shares one customer's credential with another customer. Provider usage and charges belong to the customer account that issued the key. The access checker explains when a separate owner or organization must approve access, and the wizard can accept a pending repository invitation after GitHub issues it. A configured GitHub App remains an optional additional connection mode. See `docs/BRAIN.md` for the full architecture, data model, and threat model.

At workflow selection, choose **Manual / no AI** to operate without an AI credential or provider call. This choice is stored independently for each repository and enforced by the backend. Switching back to AI requires explicitly activating a valid repository Brain Profile.

Hosted AI access and chat subscriptions are separate products. ChatGPT Plus/Pro or Codex access does not automatically provide OpenAI Platform API billing for calls made by GitPilot, and a DeepSeek chat subscription does not fund the DeepSeek API. The wizard makes this distinction explicit. For no hosted-model charge, use **Manual / no AI** or connect an Ollama/vLLM-style local server through **OpenAI-compatible / local**. DeepSeek V4 Flash is the lowest-cost hosted option in the built-in catalog, but it still requires a DeepSeek Platform API key and available balance.

Users without an API key can choose **Use external chat (copy/paste)**. This is a separate manual fallback, not a ChatGPT/Claude account connection: GitPilot cannot sign in to or operate the user's subscription. It builds a bounded, copy-ready prompt containing only the selected issue evidence and allow-listed verification commands. The user pastes that prompt into an external assistant, then pastes the returned JSON proposal back into GitPilot. The imported result is untrusted: it must pass the same file-scope, exact-match, patch-size, verification-command, proposal-hash, human-approval, isolated-branch, and test gates as an API-generated proposal. GitPilot makes no provider call and records zero model cost for this handoff.

Once a repository's brain is active, starting a guided fix from an issue walks it through **evidence → diagnosis → patch proposal → approve & apply (isolated branch) → verify → approve & publish (draft PR)** — every stage shown in the dashboard's pipeline panel, and every filesystem/GitHub write gated behind its own explicit human approval. See `docs/BRAIN.md` §16 for exactly what the AI can and cannot do at each stage.

### Hosted multi-tenant login (optional, beta)

By default the dashboard runs in local, single-owner mode — no login,
identical to today. To let multiple people log in with their own GitHub
account instead:

1. Create a GitHub OAuth App at `https://github.com/settings/developers`.
   Set its Authorization callback URL to `http://<your-host>/auth/callback`.
2. Set `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET` in `.env`
   from that app.
3. Generate a `GITPILOT_MASTER_KEY` (see `.env.example`) — this encrypts
   every logged-in user's GitHub token at rest. Never commit it or share it
   outside your deployment's secret storage.
4. Restart the dashboard. Visitors now see a "Log in with GitHub" screen.

Project memory and fix-run history are scoped to the logged-in user in
hosted mode. Local code editing remains deliberately unavailable to hosted
users because a hosted service cannot safely access a customer's disk; run
the MCP client locally for code operations.

Native GitPilot accounts are independent from the optional GitHub OAuth flow. A customer can first create an email/password workspace and then paste a fine-grained GitHub personal access token into the repository wizard. OAuth can remain disabled for a local-only deployment.

For the strongest customer onboarding, configure a public GitHub App and set `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, and `GITHUB_APP_PRIVATE_KEY_PATH`. GitHub Apps are preferred because customers choose specific repositories and the app receives fine-grained permissions and short-lived installation tokens. OAuth remains a supported fallback. An encrypted fine-grained personal access token remains available for local development where neither integration is configured.

Optional `GITPILOT_INPUT_USD_PER_MTOK` and `GITPILOT_OUTPUT_USD_PER_MTOK`
values control the provider-neutral planning estimate shown before a run.
Actual provider billing remains authoritative.

## Claude MCP

Register once:

```cmd
claude mcp add gitpilot -- "C:\Projects - Building\GithubPilotMCP\.venv\Scripts\python.exe" "C:\Projects - Building\GithubPilotMCP\server.py"
```

Example prompt:

```text
Start an AI fix run for issue #12 in atlas-library-ai. Recall project memory, inspect only the ranked files, make the smallest safe edit, verify it, save the resolution, and only then close the issue.
```

The manual flow uses `ai_start_issue_fix`, `ai_read_file`, `ai_edit_file`, `ai_verify_fix`, and `ai_complete_issue_fix`. When a repository has an active Brain Profile, the guided-fix flow (`ai_generate_patch_proposal`, `ai_approve_and_apply_patch`, `ai_verify_fix`, `ai_publish_fix`) drives the same evidence-gathering and human-approval boundaries through an AI-authored patch instead of manual edits. Arbitrary shell commands are never accepted.

Manual exact edits are hashed for audit, applied only on a dedicated `gitpilot/fix-*` branch, and can use the same verified draft-pull-request publisher. AI-generated patches require the exact reviewed proposal hash; omitting it fails closed.

## Docker

```cmd
docker compose up --build
```

The image runs as a non-root user with a read-only filesystem. SQLite persists under `data/` and approved code is mounted separately.

## Test

```cmd
python -m pytest -q
```

This runs the full offline suite only — every live-credential test is opt-in and skipped by default (`tests/test_live_integration.py`, `tests/test_live_guided_fix_integration.py`). See their module docstrings for exact setup instructions if you want to exercise a real GitHub App installation and a real AI provider end to end.

See `docs/ARCHITECTURE.md` for the memory model, state machine, provider boundaries, and production guidance, and `docs/BRAIN.md` for the GitHub App connection, AI provider vault, Repository Brain Profile, and guided-fix patch pipeline. Never commit `.env` or expose tokens in logs or screenshots.
