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

## Hosted policy review (Nov 15, from the migrations; project is paused)

The hosted project is paused, so the review used the repository's Supabase
migrations rather than live catalog queries. Findings:

- Every table policy (`seo_qa_tool`, requirements, versions, runs, observations,
  reviews, operations, events, imports, fixture destinations, knowledge pins,
  scoped checkpoints, browser sessions) is `auth.uid() = owner_id`.
- Every `security definer` workflow function filters on `auth.uid()`.
- Evidence storage reads/writes are limited to the caller's own `auth.uid()` folder.
- No fixed owner UUID appears in any policy or function.
- `supabase/config.toml` sets `enable_signup = false`, so only accounts the owner creates exist.
- Knowledge bundles are readable by users listed in `seo_knowledge_readers`;
  add members there only if they should read published knowledge.

Conclusion: the schema already isolates accounts; no policy migration is needed
for allowlisted members. The live project must still be confirmed to match the
migrations when it is resumed.

## Onboarding a member

A member has no storage root until one is created. Root creation is explicit
and owner-run, never automatic at sign-in:

1. Create the member's Auth user in the Supabase dashboard (signups stay off).
2. Add their user id to `SEO_MEMBER_ACCOUNT_IDS` and redeploy.
3. With the member's own session token, call `BrowserStorage.provision_root(token)`.
   RLS only permits a root whose `owner_id` equals the caller.

## Remaining gates before shared hosting (not done here)

1. **Resume and confirm.** When hosting resumes, confirm live policies match the
   migrations (read-only `pg_policies` query).
2. **Real second account.** Create a genuine second Auth user and repeat the
   isolation checks against the hosted project.
3. **Account lifecycle.** Invitation, removal and data-retention decisions for
   members are product/policy choices still open.
4. **Hosting budget and launch approval.** Hosting stays paused; spending $0.
