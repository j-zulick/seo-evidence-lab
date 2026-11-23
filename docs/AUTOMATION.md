# Autonomous release pipeline

Agents (Claude, Hermes, Codex) ship this repository without the owner in the loop
for routine work. GitHub enforces the gates. Agents don't have to be trusted to follow them.

## Flow
1. An agent works on a branch named `<agent>/<topic>` (e.g. `claude/guidance-release`).
2. It opens a pull request to `main` and turns on auto-merge.
3. `Local fixtures` CI runs on the PR. `main` is protected: the `fixtures` check must
   pass before anything merges. No force pushes, no branch deletion.
4. To release, the PR also adds `docs/releases/vX.Y.Z.md`. Agents never push tags
   or upload assets.
5. When that file lands on `main`, the `Release` workflow refuses if the notes contain
   FILL/TODO/TBD, lack a `## Boundaries` section, or more than one version is pending. Otherwise it
   re-runs CI on the released commit (default: the merge; or the commit named in a
   `<!-- release-commit: SHA -->` first-lines marker, which must be on `main`). Then it
   tags it, builds the source ZIP and SHA256SUMS with `git archive`, publishes, and
   downloads the asset again to check its hash. Existing tags are never moved.

## What automation must never do
- Record a review, approval, label or acceptance in the owner's name, or call agent-made
  labels independent. Release notes say what is still pending in `## Boundaries`.
- Spend money, resume hosting, create accounts, or make live provider/model/Ahrefs calls.
  Hosting stays paused until the owner sets a budget.
- Commit private data: saved exports, SQLite stores, receipts, `/Users/` paths, tokens.
- Rewrite published tags, releases or history.

## Versioning
- Patch (`v0.2.x`): fixes and docs. Minor (`v0.x.0`): new milestone features.
- Features whose acceptance needs a human ship as "groundwork" or "preview" and say so.

## Owner controls
- Pause all releases: set repo variable `RELEASES_PAUSED=true`
  (Settings → Secrets and variables → Actions → Variables).
- Pause merges: turn off "Allow auto-merge" in Settings → General.
- Undo a release: delete the GitHub release and tag. Revert the merge commit on `main` with a PR.
