#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q tests/test_competitor.py tests/test_measurement_mcp.py tests/test_overnight.py tests/test_botworthiness_app.py tests/test_botworthiness_local.py tests/test_site_workspace.py tests/test_g06_exports.py tests/test_knowledge_export_privacy.py tests/test_web.py tests/test_g02_integration.py tests/test_browser_storage.py tests/test_release_recovery.py tests/test_knowledge_cli.py
.venv/bin/python setup/check_local_boundary.py
.venv/bin/python -m seo_qa.fixture_walkthrough --output "${SEO_DEMO_OUTPUT:-demo-output}"
