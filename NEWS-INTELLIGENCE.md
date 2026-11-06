# News intelligence and research
The intended product differentiator is a custom SEO and LLM-visibility news workflow that turns discoveries into reviewable, versioned guidance and explains which audit findings change.

## Present implementation
The local app imports saved page captures, compares versions and replays a heading criterion. Community-news fixtures show how a subreddit post becomes a lead, a linked publisher document becomes a source to inspect, and a removed observation loses its displayed content. The current view reads synthetic packaged observations. Provider authentication, a connected news-agent feed, real subreddit capture and unattended collection require separate work.

The user's existing news workflow and web-research/scraper projects are separate systems. Their handoff documents preserve source lists, deduplication and coverage requirements. This repository's demo does not establish an accepted live bridge.

## Integration contract to build next
1. Normalize incoming news observations with source URL, origin (publisher/community), publication time when known, observation time, topic and source-content hash.
2. Deduplicate stories while retaining independent source identities. Preserve failures and late arrivals.
3. Route high-signal SEO/LLM-visibility claims to a review inbox. Link primary documents and distinguish contradictions.
4. Publish approved guidance into an immutable release. A new release must preserve earlier audit pins and human review provenance.
5. Link each source change to affected findings and expose the comparison over read-only MCP.
6. Measure retrieval coverage, article freshness, deduplication, reviewer agreement and source-to-reviewed-release delay.

## Research advantage
Use documented examples to show information discovered through specialist feeds, subreddit discussions, internal site search, linked documentation or pages with weak public-search visibility. Record query, source, timestamp and capture. Search-engine absence is a bounded observation about a dated query; it does not establish universal invisibility or permission to access restricted content.

## Reddit
Live API collection requires provider approval under the Responsible Builder Policy. Request the exact read-only use case and verify applicable access/cost limits. Keep provider credentials private. The fixture demo requires zero live calls. Retrieval through the custom scraper must follow the same access constraints.
Plan bounded subreddit profiles, observation windows and comment depth. Treat engagement as prioritization metadata, not proof of a claim. Handle removals, retention and deletion obligations before storing live data. Do not publish usernames or copied discussion archives in the public demo.

## Public-release decisions
Select repository and license after provenance review. Push the honest initial baseline and real subsequent feature/CI/documentation commits together, then push future verified changes as they occur. Keep timestamps authentic. A release tag should identify an actual source revision and its accepted fixture checks.
