# Multi-account identity and route scoping (Iteration 2, local)

Status: implemented and tested offline on a local branch. Not deployed. No hosted
schema, policy or account was created or changed.

## Contract

- **Allowlist, never implicit.** `SEO_SUPABASE_OWNER_ID` is always allowed.
  Extra accounts are allowed only when listed in `SEO_MEMBER_ACCOUNT_IDS`
  (comma-separated canonical UUIDs, at most 25, unique, not the owner).
  Unset or blank keeps the previous single-owner behavior exactly.
  Malformed lists refuse configuration before any I/O (`seo_qa/accounts.py`).
- **Identity comes from the verified token.** `BrowserStorage` selects the
  account from the token subject, refuses unlisted subjects, and the Supabase
  Auth user endpoint must return the same id before any cookie or Store exists.
  A provider response naming a different user than the token is refused.
- **Cookie and token must agree.** `BrowserAuth` checks the signed cookie's
  account against the backend's `token_account(token)` at sign-in and on every
  request. Removing an account from the allowlist invalidates its open sessions
  on the next request.
- **Per-account sessions.** Session rows are written, read and revoked under the
  signed-in account's `owner_id`. A session hash presented with another
  account's token finds no row.
- **Per-account storage.** Each account resolves its own storage root
  (`uuid5(project_ref:account_id)`). Members never resolve the owner's root.
  Shared client workspaces stay with `client_workspace` memberships and the
  existing `/clientops` adapter, which already authorizes by principal.

## Route scoping

`tests/test_multi_account.py` enumerates every route in the browser production
composition and fails if any route is unclassified:

| Class | Rule | Examples |
|---|---|---|
| Public | no session | `/health`, `/login`, `/logout` |
| Account-scoped | served only from the per-request, per-account Store | `/`, `/requirements/*`, `/runs/*` incl. reports, `/imports*`, `/qa`, `/qa/audits*` incl. explanations |
| Local-only | refused (4xx/503) in any production/browser composition | site workspace, botworthiness, saved diagnostics, news imports, lab/walkthrough/evaluations, source inbox, community-retained |

MCP servers (`selected_dataset_mcp`, `timeline_mcp`) are local stdio tools bound
to one startup-selected dataset. They are never mounted in the web app; the test
asserts no MCP route exists. Hosting them would need its own reviewed contract.

Two-account tests cover: listing, detail, run creation, all report formats,
QA audit views and explanations, CSRF transfer, cookie forgery with a different
account, unlisted sign-in, and allowlist removal.

## Remaining gates before shared hosting (not done here)

1. **Hosted policy review.** Confirm Supabase RLS on `seo_qa_tool`,
   `seo_browser_sessions`, run/evidence tables and the evidence bucket keys on
   `auth.uid() = owner_id` (not a fixed owner UUID), and that a member can
   create only its own root row. Read-only check, then a reviewed migration if needed.
2. **Real second account.** Create a genuine second Auth user and repeat the
   isolation checks against the hosted project.
3. **Account lifecycle.** Invitation, removal and data-retention decisions for
   members are product/policy choices still open.
4. **Hosting budget and launch approval.** Hosting stays paused; spending $0.
