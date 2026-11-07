# Security and operating scope

This release is a local evidence-review application and synthetic demonstration. It is not accepted as a public multi-tenant service. The separately protected hosted preview is an inert static export, not the full application.

Keep the local server on loopback. Do not expose it publicly or enable historical hosted adapters as part of the quickstart. Supply only content and observations you are permitted to process. Free-text exports can retain private information even when structured raw inputs are omitted; inspect exports before sharing.

Keep credentials, environment files, private captures, databases, dependency environments, and internal review receipts outside Git. The repository's ignore rules do not establish that historical commits contain no sensitive data. Review the full reachable history and screenshots/recordings before publication.

The standard fixture walkthrough needs no live Ahrefs or model calls. Installing dependencies may use the package registry. Live-provider authentication, schedules, dynamic hosting, and account grants require separate scope. Local passing tests do not constitute a penetration test, comprehensive security audit, or dependency-vulnerability certification.

For a suspected security issue, do not open an issue containing credentials, private records, or an exploit against a third-party system. Use a verified private contact channel published by the repository owner. If none is available, request a private reporting route without sensitive details. No response-time commitment or managed security service is implied.
