#!/bin/sh
set -eu
cd "$(dirname "$0")"
runtime="${SEO_QA_PYTHON:-$PWD/.venv/bin/python}"
if [ ! -x "$runtime" ]; then
  echo "Choose a compatible Python environment with SEO_QA_PYTHON, or create this project's .venv."
  exit 1
fi
export SEO_SUPABASE_ADAPTER_ENABLED=false
export SEO_KNOWLEDGE_ENABLED=false
echo "Open http://127.0.0.1:${SEO_QA_LOCAL_PORT:-8047}/qa"
exec "$runtime" -m seo_qa qa --state state/local.sqlite --port "${SEO_QA_LOCAL_PORT:-8047}"
