# Dependency license inventory

The application source does not vendor these dependency packages. The pinned environment installs them separately; each package retains its own license. This inventory records installed metadata for the 72 versions in `requirements-demo.lock`, including development and transitive dependencies. An installed package is not necessarily a product feature.

The proposed MIT license applies to project-owned code only. It does not relicense dependency code, third-party methodologies, provider data, trademarks or linked source material. If distributing a bundled environment or binary later, review and include the corresponding package license/notice files and any additional distribution obligations. No bundled environment/browser binaries are included in the source archive.

| Package | Pinned version | License reported by installed distribution |
| --- | --- | --- |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.16.0 | MIT |
| attrs | 26.1.0 | MIT |
| certifi | 2026.10.23 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| distro | 1.9.0 | Apache License, Version 2.0 |
| fastapi | 0.162.0 | MIT |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.16.0 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpx-sse | 0.4.3 | MIT |
| httpx2 | 2.16.0 | BSD-3-Clause |
| hypothesis | 6.170.14 | MPL-2.0 |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.1 | MIT |
| Jinja2 | 3.1.6 | License :: OSI Approved :: BSD License |
| jsonpatch | 1.43 | BSD-3-Clause |
| jsonpointer | 3.9 | Modified BSD License |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| langchain-core | 1.7.6 | MIT |
| langchain-protocol | 0.0.21 | MIT |
| langgraph | 1.2.19 | MIT |
| langgraph-checkpoint | 4.3.1 | MIT |
| langgraph-prebuilt | 1.1.1 | MIT |
| langgraph-sdk | 0.4.14 | MIT |
| langsmith | 0.14.17 | MIT |
| MarkupSafe | 3.0.4 | BSD-3-Clause |
| mcp | 1.30.0 | MIT |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| opentelemetry-sdk | 1.45.2 | Apache-2.0 |
| opentelemetry-semantic-conventions | 0.66b2 | Apache-2.0 |
| orjson | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| ormsgpack | 1.13.0 | Apache-2.0 OR MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| playwright | 1.64.1 | Apache-2.0 |
| pluggy | 1.7.0 | MIT |
| pycparser | 3.12 | BSD-3-Clause |
| pydantic | 2.14.2 | MIT |
| pydantic-settings | 2.16.2 | MIT |
| pydantic_core | 2.50.2 | MIT |
| pyee | 14.0.0 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| PyJWT | 2.15.1 | MIT |
| pytest | 9.2.0 | MIT |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| python-multipart | 0.0.33 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT |
| referencing | 0.37.0 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| requests-toolbelt | 1.0.0 | Apache 2.0 |
| rpds-py | 2026.9.1 | MIT |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| sortedcontainers | 2.4.0 | Apache 2.0 |
| sse-starlette | 3.6.0 | BSD-3-Clause |
| starlette | 1.10.0 | BSD-3-Clause |
| tenacity | 9.2.1 | Apache-2.0 |
| truststore | 0.10.5 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.8.0 | MIT |
| uuid_utils | 0.19.0 | BSD-3-Clause |
| uvicorn | 0.54.0 | BSD-3-Clause |
| websockets | 17.4 | BSD-3-Clause |
| xxhash | 4.0.2 | BSD-2-Clause |
| zstandard | 0.25.0 | BSD-3-Clause |

Jinja2 and jsonpointer use legacy metadata labels above; their installed license files are BSD-style notices and must remain with those distributions. Certifi and Hypothesis report MPL-2.0; orjson reports a combined expression. Those are retained dependency obligations, not licenses assigned to this repository. Metadata inspection is not an automated compatibility opinion or vulnerability audit.

The private release review pins the inspected metadata and license-file hashes. This public inventory excludes local paths and private review receipts.
