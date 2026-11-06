"""Account-free build preflight for the explicit Vercel runtime source package.

Checks source and installed dependency identity without importing the app,
loading credentials, opening databases or making requests.
"""
from __future__ import annotations
import ast
import importlib.metadata
import json
from pathlib import Path
import re

RUNTIME_MODULES = (
    'crawl_uploads', 'architecture_analysis', 'architecture_upload',
    'performance_analysis', 'performance_upload', 'explanation_presenter', 'site_workspace', 'workspace_exports',
    '__init__', 'production', 'production_storage', 'web', 'simple_qa',
    'scope_criteria', 'models', 'store', 'inspector', 'csv_export',
    'markdown_export', 'scoring', 'connection_web', 'supabase_auth', 'supabase_adapter',
    'connection_config', 'hosted_store', 'imports',
    'browser_auth', 'browser_storage', 'request_context', 'worker',
    'scoped_storage', 'scoped_workflow',
)
RUNTIME_FILES = (
    'app.py', 'vercel_build.py', 'vercel.json', 'requirements.txt',
    'requirements-runtime.lock', '.python-version', '.vercelignore',
    'templates/app.html', 'templates/qa.html', 'templates/scoring.html', 'templates/site_workspace.html', 'templates/explanation_modes.html',
    *(f'seo_qa/{name}.py' for name in RUNTIME_MODULES),
    *(f'seo_qa/knowledge/{name}.py' for name in (
        '__init__', 'contracts', 'policy', 'archive', 'repository', 'retrieval',
        'audit_pin', 'remote', 'config', 'hosted', 'freshness')),
)


def pinned_requirements(root):
    manifest = {}
    for line in (root/'requirements.txt').read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^\s;]+)',line)
        if match is None:
            raise ValueError('Runtime manifest must use exact pins without index overrides')
        name, version = match.group(1,2)
        key = name.lower().replace('_','-')
        if key in manifest:
            raise ValueError('Duplicate runtime dependency')
        manifest[key] = version
    lines = (root/'requirements-runtime.lock').read_text().splitlines()
    if '--require-hashes' not in lines or '--only-binary=:all:' not in lines:
        raise ValueError('Runtime lock must enforce wheel hashes')
    result = {}
    for line in lines:
        if not line.strip() or line.startswith('#') or line in {'--require-hashes','--only-binary=:all:'}:
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^\s;]+)((?: --hash=sha256:[a-f0-9]{64})+)', line)
        if match is None:
            raise ValueError('Unpinned or unsupported runtime requirement')
        name, version = match.group(1, 2)
        normalized = name.lower().replace('_', '-')
        if normalized in result:
            raise ValueError('Duplicate runtime dependency')
        result[normalized] = version
    for name in ('fastapi','pydantic','httpx','jinja2','python-multipart','uvicorn'):
        if name not in result:
            raise ValueError('Required runtime dependency missing: '+name)
    if manifest != result:
        raise ValueError('Runtime pins do not match the artifact hash lock')
    return result


def check(root, *, installed=True):
    root = Path(root)
    if root.is_symlink():
        raise ValueError('Symlinked source root refused')
    root = root.resolve()
    # This successor adds local-only imports outside the historical hosted allowlist.
    # Refuse both direct builds and packaging until an explicitly reviewed hosted
    # composition replaces this local release boundary.
    for entry in ('seo_qa/web.py', 'seo_qa/simple_qa.py'):
        if 'from .botworthiness.local_web import' in (root/entry).read_text():
            raise ValueError('Local-only integrated release: hosted packaging is disabled; use the separately selected hosted source')
    for name in RUNTIME_FILES:
        path = root/name
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
            raise ValueError('Symlinked runtime source refused')
        if not path.is_file():
            raise ValueError('Required runtime source missing: '+name)
        if path.suffix == '.py':
            ast.parse(path.read_bytes(), filename=name)
    config = json.loads((root/'vercel.json').read_text())
    if config.get('framework') != 'fastapi' or config.get('regions') != ['cle1']:
        raise ValueError('Unexpected FastAPI framework or region')
    if any(k in config for k in ('builds','routes','rewrites','redirects','crons','schedules')):
        raise ValueError('Legacy routing or schedules are not part of this package')
    if config.get('buildCommand') != 'python vercel_build.py':
        raise ValueError('Build preflight is required')
    functions = config.get('functions', {})
    if set(functions) != {'app.py'} or functions['app.py'].get('maxDuration') != 60:
        raise ValueError('Unexpected function entry point or duration')
    if functions['app.py'].get('includeFiles') != 'templates/**':
        raise ValueError('Both server-rendered templates must be bundled')
    if 'state/**' not in functions['app.py'].get('excludeFiles', ''):
        raise ValueError('Private state must be excluded')
    if (root/'.python-version').read_text().strip() != '3.12':
        raise ValueError('This candidate targets Python 3.12')
    requirements = pinned_requirements(root)
    if installed:
        for name, version in requirements.items():
            try:
                actual = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                raise ValueError('Runtime dependency missing: '+name) from None
            if actual != version:
                raise ValueError('Runtime dependency version mismatch: '+name)
    return {'entrypoint':'app.py:app','framework':'fastapi','python':'3.12',
            'region':'cle1','duration_seconds':60,'runtime_files':len(RUNTIME_FILES),
            'locked_distributions':len(requirements),'installed_versions_checked':installed,
            'credentials_read':False,'network_calls':False}


if __name__ == '__main__':
    print(json.dumps(check(Path(__file__).resolve().parent),sort_keys=True))
