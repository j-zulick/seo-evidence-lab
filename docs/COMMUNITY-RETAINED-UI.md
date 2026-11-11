# Retained community leads in the local app

Start the local app normally and choose **News leads → Open privately retained saved leads**, or open `/qa/community-retained`. The existing packaged synthetic news screen remains separate.

Paste a saved permitted community JSON export and choose 1–90 retention days. Imports never fetch linked sources, approve guidance, or enable collection. The envelope requires an explicit `synthetic` boolean and `posts` list. Each active post requires an ID, title, subreddit, timezone-bearing created/observed dates and optional HTTPS `primary_sources`. Dates must be ordered and observation cannot follow the actual receipt time. The application clock supplies receipt and removal times; the form cannot override them.

Example format (replace dates with genuine observation timestamps; old examples may correctly expire):

```json
{"synthetic":true,"posts":[{"id":"example-1","title":"Synthetic review lead","subreddit":"fixture","created_at":"2026-11-11T01:00:00Z","observed_at":"2026-11-11T02:00:00Z","primary_sources":["https://example.org/guide"]}]}
```

The lead list labels declared synthetic versus saved external provenance and preserves review status. A user-supplied label does not authenticate the source. Titles and IDs are escaped; links are shown as text for manual inspection. Raw discussion bodies/usernames are excluded from normalized storage. No guidance decision is inferred.

**Remove retained content** scrubs the private stored body and leaves a sticky identity tombstone. A later import cannot revive it. Expired text is scrubbed during reads/imports. Updating an observation cannot extend initial retention. Existing private backups or separately retained export files require their own deletion handling.

Storage is an owner-local SQLite sidecar beside the selected app database, named `<state database>.community.sqlite`, mode 0600. Include this database when deliberately making an isolated stopped-writer private backup; never publish it. Removal does not delete the user's source export or older backup copies.

The page is unavailable in production mode, browser-auth mode and nonlocal store modes, using the existing owner-local availability boundary. Mutations require the current form token. Saved JSON is limited to 500 KB and the encoded form to 3.1 MB; duplicate form fields are refused. Responses containing retained leads disable caching.

Verification covers actual web import, restart readback, expiry/removal, late-import suppression, generic errors, escaping, access boundaries, request limits, CSRF and bad hosts. Chrome exercises desktop/mobile import and removal with external requests blocked. This is offline local preparation, not live community/provider or hosted acceptance.
