# Security Policy

## Secrets

- Store a GitHub token only through the dashboard's encrypted credential vault, `.env`, or a deployment secret manager.
- Never commit `.env`, paste tokens into chat, or include them in screenshots.
- Prefer a fine-grained GitHub token restricted to the intended repositories. Grant Metadata read, Contents read/write, Issues read/write, and Pull requests read/write only when the guided-fix workflow needs them.
- Rotate a token immediately if it is exposed.
- Dashboard-connected GitHub tokens are validated before storage, encrypted at rest, and never returned by an API response. Local loopback installs use a private ignored vault-key file; production, staging, network-exposed, and OAuth deployments require `GITPILOT_MASTER_KEY` from secret storage.

## Customer Accounts and API Keys

- Native account passwords are normalized only for email lookup, then salted independently and one-way hashed with scrypt. Plaintext passwords are never stored, encrypted, logged, or returned.
- Password recovery uses hashed, single-use tokens with a 20-minute expiry. Successful resets clear lockouts and invalidate older login sessions; production delivery requires SMTP, while direct recovery is restricted to loopback development.
- Five failed password attempts temporarily lock authentication for five minutes. Login failures use a generic response so they do not reveal whether an email is registered.
- Every native account has an opaque UUID used to scope GitHub tokens, AI-provider credentials, Brain Profiles, memory, and fix runs.
- The typed GitHub profile name is an expected identity only. OAuth or PAT validation supplies the authoritative GitHub login, and the profile is marked verified only after that confirmation.
- Repository visibility comes only from GitHub's API response for the connected token or App installation. Typing a profile or repository name never grants access.
- GitPilot never renders fields for a GitHub password or GitHub OTP. Customers enter those credentials only on `github.com`; GitPilot's OAuth flow uses state validation and PKCE before binding the encrypted result to the native customer UUID.
- GitHub App installation is preferred for market deployments because GitHub displays the requested permissions and lets the customer select repositories. Organization-owner approval remains authoritative and cannot be bypassed by GitPilot.
- GitPilot product API keys use the `gp_live_` prefix, are generated from cryptographically secure randomness, and are displayed only once. SQLite stores only a SHA-256 digest and a short identification prefix.
- Product API keys can be revoked immediately. Creating, listing, or revoking keys requires an authenticated browser session; a bearer key cannot create additional keys.
- Never create executable `.py` files or per-customer environment files containing credentials. Use the encrypted, owner-scoped credential tables and a deployment secret manager for the master key.

## Deployment

The dashboard binds to `127.0.0.1` by default. Keep it local unless an authenticated reverse proxy is added. The included Compose configuration publishes only to localhost and runs the container as a non-root user with a read-only filesystem. For customer deployment, set `GITPILOT_REQUIRE_ACCOUNT_LOGIN=true`, provide a persistent independent `GITPILOT_SESSION_SECRET`, enable `GITPILOT_HTTPS_ONLY=true`, and terminate traffic through HTTPS.

## Repository Boundary

GitPilot lists repositories that GitHub says the authenticated user can access, including owned, collaborator, and organization repositories. Repository keys retain the owner so similarly named repositories cannot share Brain or memory state.

GitPilot cannot grant itself access to another person's private repository. The repository owner must invite the user, an organization administrator may need to approve a fine-grained token, or an authorized owner must install the GitHub App. The dashboard can show that approval is required and accept an invitation after GitHub has issued it, but it never bypasses GitHub's authorization decision.

Repository indexing fails closed on sensitive path classes (`.env*`, secret/token/credential directories, and common plaintext secret filenames), private-key headers, recognized provider-issued credential shapes, and high-confidence secret assignments in configuration/data files. The same content filter runs for direct read and edit requests, preventing path-based bypass. Exclusion counts may be shown; secret paths and values are not.

Manual no-AI mode is a backend-enforced, repository- and tenant-scoped setting rather than a cosmetic frontend toggle. While selected, diagnosis and patch-proposal provider calls fail before budget reservation, credential decryption, or network access. Manual exact edits still require explicit old/new text plus a reason, are hashed into the audit trail, and are applied only on an isolated branch.

## AI Provider Keys

- Connected only through the dashboard's "Connect repository and select your brain" wizard, never through `.env` or any config file.
- Encrypted at rest with the same `GITPILOT_MASTER_KEY`/Fernet mechanism (`services/crypto.py`) already used for GitHub OAuth tokens, including comma-separated key rotation.
- Validated with the provider's model-list endpoint before being stored; an invalid key is never persisted.
- Never returned by any API response, log line, exception message, or audit record — only a redacted label, provider, and validation status.
- Revocable anytime via `DELETE /api/ai-providers/{credential_id}`.

## GitHub App Private Key

- Referenced only by file path (`GITHUB_APP_PRIVATE_KEY_PATH`) — the PEM contents are never stored in SQLite or the database.
- Used only to sign short-lived (~9 minute) App JWTs server-side, which are in turn exchanged for short-lived (~1 hour) installation access tokens. Installation tokens are cached in memory only and never persisted.
- Restrict the key file's permissions on disk (owner read-only) and treat it as you would `GITHUB_TOKEN` — rotate immediately if exposed, via the GitHub App's own settings page.
- Webhook deliveries (`POST /api/webhooks/github-app`) are verified against `GITHUB_APP_WEBHOOK_SECRET` using HMAC-SHA256 (`X-Hub-Signature-256`) before any installation state is changed; a missing or invalid signature is rejected with 401.

See `docs/BRAIN.md` for the full threat model and data flow.

## Guided-Fix AI Patch Pipeline

- An AI-generated patch proposal is treated as untrusted structured input, never as trusted code: it is JSON-schema-validated (`services/patch_validation.py`), and every `old_text` change must exact-match unique, real file content already shown to the model as evidence — the AI cannot select an arbitrary path or reference code it was never shown.
- A proposal can request only verification commands already on the repository's own trusted allow-list; it can never invent or substitute a shell command.
- Applying a patch requires an explicit human approval that references the exact content hash of the reviewed proposal; a changed or regenerated proposal invalidates a stale approval.
- Edits always land on an isolated `gitpilot/fix-<run_id>-...` branch (`services/git_isolation.py`). Protected branches (`main`, `master`, and configured prefixes) can never be edited directly, and a dirty protected branch blocks branch creation rather than being silently branched from.
- Publishing (pushing the branch and opening a pull request) requires a second, separate explicit approval and always opens the pull request as a **draft** — this pipeline never merges anything; merging remains a human action on GitHub.
- Closing the originating issue requires both a passing verification and an explicit approval — never one without the other.
- Every push and pull-request creation is idempotent via the same `run_operations` ledger used for comment/close idempotency, so a retried publish after a crash never double-pushes or opens a duplicate PR.
- The optional bounded auto-retry loop (`auto_fix_run`, capped at 5 attempts) applies each of its own proposals without a separate per-attempt human approval, but only within limits a reviewer should know about: it refuses to auto-apply any proposal rated `risk_level: "high"` or below a fixed confidence floor, stopping for ordinary human review instead; it never calls `publish_fix`, so reaching a verified state through the loop still requires the same second, separate explicit publish approval as a single-attempt run; and a failed attempt never counts as spent budget (the reservation is released, not charged).
- See `docs/BRAIN.md` §16 for the full flow, §16.6 for an explicit list of what the AI can never do in this pipeline, and §16.8 for the auto-retry loop's specific bounds.

## Reporting

Do not open public issues containing credentials, private repository names, API responses with sensitive data, or security exploit details.
