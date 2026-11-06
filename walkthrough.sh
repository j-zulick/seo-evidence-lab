#!/bin/sh
set -eu
cd "$(dirname "$0")"
./setup/bootstrap_local.sh
exec .venv/bin/python -m seo_qa.fixture_walkthrough --output "${SEO_DEMO_OUTPUT:-demo-output}"
