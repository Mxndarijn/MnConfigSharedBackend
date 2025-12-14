import json
import os
import time
from copy import deepcopy
from dataclasses import dataclass, asdict
from fnmatch import fnmatch
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Query, Body, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from jsonschema import Draft202012Validator

# -----------------------
# Config & setup
# -----------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
SCHEMA_DIR = os.path.join(os.path.dirname(__file__), 'schemas')
STORE_PATH = os.path.join(DATA_DIR, 'config_store.json')
REGISTRY_PATH = os.path.join(SCHEMA_DIR, 'registry.json')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SCHEMA_DIR, exist_ok=True)

app = FastAPI(title="Mn Config Dev Backend", version="1.0.0", description="Development-only backend that stores and serves runtime component configurations for Angular apps.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev only; tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------
# Types
# -----------------------
@dataclass
class ConfigDoc:
    tenant: str
    env: str
    componentKey: str
    scopeType: str  # 'global' | 'route' | 'page'
    scopeKey: str   # '*' or route pattern like '/products/*' or page id
    version: int
    value: Dict[str, Any]
    createdAt: float
    createdBy: str  # dev string

# -----------------------
# In-memory store + file persistence
# -----------------------
_store: List[ConfigDoc] = []
_registry: Dict[str, Dict[str, Any]] = {}


def load_registry():
    global _registry
    if os.path.exists(REGISTRY_PATH):
        with open(REGISTRY_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            _registry = data.get('components', {})
    else:
        _registry = {}


def load_store():
    global _store
    if os.path.exists(STORE_PATH):
        with open(STORE_PATH, 'r', encoding='utf-8') as f:
            raw = json.load(f)
            _store = [ConfigDoc(**item) for item in raw]
    else:
        _store = []


def save_store():
    with open(STORE_PATH, 'w', encoding='utf-8') as f:
        json.dump([asdict(d) for d in _store], f, indent=2)


load_registry()
load_store()

# -----------------------
# Helpers: merge & selection
# -----------------------

def deep_merge(base: Any, override: Any) -> Any:
    if override is None:
        return base
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = deep_merge(base.get(k), v)
        return out
    # For arrays and primitives: replace
    return override


def default_for(component_key: str) -> Dict[str, Any]:
    entry = _registry.get(component_key)
    return deepcopy(entry.get('default')) if entry and 'default' in entry else {}


def validate_value(component_key: str, value: Dict[str, Any]) -> Optional[List[str]]:
    entry = _registry.get(component_key)
    if not entry or 'schema' not in entry:
        return None  # no validation
    schema = entry['schema']
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda e: e.path)
    if not errors:
        return None
    return [f"{list(e.path)}: {e.message}" for e in errors]


def sort_scopes(scopes: List[ConfigDoc]) -> List[ConfigDoc]:
    # Specificity: global < page (exact) < route (longer pattern is more specific)
    def score(doc: ConfigDoc) -> Tuple[int, int]:
        if doc.scopeType == 'global':
            return (0, 0)
        if doc.scopeType == 'page':
            # exact match only; treat as medium specificity
            return (1, len(doc.scopeKey))
        if doc.scopeType == 'route':
            # longer patterns considered more specific
            return (2, len(doc.scopeKey))
        return (0, 0)
    return sorted(scopes, key=score)


def match_route(pattern: str, route: str) -> bool:
    # basic wildcard match: '/products/*' etc.
    return fnmatch(route, pattern)


def latest_versions_for(identity: Tuple[str, str, str]) -> List[ConfigDoc]:
    tenant, env, component_key = identity
    candidates = [d for d in _store if d.tenant == tenant and d.env == env and d.componentKey == component_key]
    # Keep latest per (scopeType, scopeKey)
    latest: Dict[Tuple[str, str], ConfigDoc] = {}
    for d in candidates:
        key = (d.scopeType, d.scopeKey)
        prev = latest.get(key)
        if prev is None or d.version > prev.version:
            latest[key] = d
    return list(latest.values())


def effective_for_component(tenant: str, env: str, component_key: str, route: Optional[str] = None, page: Optional[str] = None) -> Dict[str, Any]:
    effective = default_for(component_key)
    docs = latest_versions_for((tenant, env, component_key))
    # Apply global, then page (matching page id), then route (matching pattern and sorted by specificity)
    globals_ = [d for d in docs if d.scopeType == 'global']
    pages_ = [d for d in docs if d.scopeType == 'page' and page and d.scopeKey == page]
    routes_ = [d for d in docs if d.scopeType == 'route' and route and match_route(d.scopeKey, route)]

    for d in sort_scopes(globals_):
        effective = deep_merge(effective, d.value)
    for d in sort_scopes(pages_):
        effective = deep_merge(effective, d.value)
    for d in sort_scopes(routes_):
        effective = deep_merge(effective, d.value)

    return effective


def effective_all(tenant: str, env: str, route: Optional[str], page: Optional[str]) -> Dict[str, Any]:
    # determine all known component keys from registry + store
    keys = set(_registry.keys())
    for d in _store:
        if d.tenant == tenant and d.env == env:
            keys.add(d.componentKey)
    out: Dict[str, Any] = {}
    for key in sorted(keys):
        out[key] = effective_for_component(tenant, env, key, route=route, page=page)
    return out

# -----------------------
# API
# -----------------------
@app.get('/api/mn-config')
def get_all_effective(
    tenant: str = Query('default', description='Tenant key'),
    env: str = Query('dev', description='Environment key'),
    route: Optional[str] = Query(None, description='Optional current URL path'),
    page: Optional[str] = Query(None, description='Optional page id'),
):
    """Get effective merged configuration for all components."""
    result = effective_all(tenant, env, route, page)
    return result


@app.get('/api/mn-config/{component_key}')
def get_component_effective(
    component_key: str,
    tenant: str = Query('default', description='Tenant key'),
    env: str = Query('dev', description='Environment key'),
    route: Optional[str] = Query(None, description='Optional current URL path'),
    page: Optional[str] = Query(None, description='Optional page id'),
):
    """Get effective merged configuration for a single component."""
    result = effective_for_component(tenant, env, component_key, route=route, page=page)
    return result


@app.get('/api/mn-config/history/{component_key}')
def get_history(
    component_key: str,
    tenant: str = Query('default', description='Tenant key'),
    env: str = Query('dev', description='Environment key'),
):
    """Get versioned history of config documents for a component (current tenant/env)."""
    docs = [asdict(d) for d in _store if d.tenant == tenant and d.env == env and d.componentKey == component_key]
    # sort by version ascending
    docs.sort(key=lambda d: (d['scopeType'], d['scopeKey'], d['version']))
    return docs


@app.put('/api/mn-config/{component_key}')
def upsert_component(
    component_key: str,
    tenant_q: Optional[str] = Query(None, description='Tenant key (query param)'),
    env_q: Optional[str] = Query(None, description='Environment key (query param)'),
    body: Dict[str, Any] = Body(..., description='Payload containing value and optional keys'),
):
    """Upsert (append) a new version for a component scope."""
    tenant = body.get('tenant', tenant_q or 'default')
    env = body.get('env', env_q or 'dev')
    scope_type = body.get('scopeType', 'global')  # 'global' | 'route' | 'page'
    scope_key = body.get('scopeKey', '*')
    value = body.get('value', {})

    # Validate (optional if schema exists)
    errors = validate_value(component_key, value)
    if errors:
        # In FastAPI, raising HTTPException sets status and returns JSON
        raise HTTPException(status_code=400, detail={"error": "ValidationError", "details": errors})

    # Compute next version
    existing = [d for d in _store if d.tenant == tenant and d.env == env and d.componentKey == component_key and d.scopeType == scope_type and d.scopeKey == scope_key]
    next_version = max([d.version for d in existing], default=0) + 1

    doc = ConfigDoc(
        tenant=tenant,
        env=env,
        componentKey=component_key,
        scopeType=scope_type,
        scopeKey=scope_key,
        version=next_version,
        value=value,
        createdAt=time.time(),
        createdBy=body.get('updatedBy', 'dev')
    )

    _store.append(doc)
    save_store()

    return {"status": "ok", "version": next_version}


# ---- Dev helpers (unsafe; dev only) ----
@app.delete('/api/mn-config')
def delete_all():
    """Delete all configuration documents (dev-only helper)."""
    _store.clear()
    save_store()
    return {"status": "cleared"}


@app.delete('/api/mn-config/{component_key}')
def delete_component(
    component_key: str,
    tenant: str = Query('default', description='Tenant key'),
    env: str = Query('dev', description='Environment key'),
):
    """Delete all documents for a component under the current tenant/env (dev-only helper)."""
    keep = [d for d in _store if not (d.tenant == tenant and d.env == env and d.componentKey == component_key)]
    _store[:] = keep
    save_store()
    return {"status": "deleted"}


@app.get('/health')
def health():
    """Health check endpoint."""
    return {"ok": True}


if __name__ == '__main__':
    # python main.py
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5050, reload=True)
