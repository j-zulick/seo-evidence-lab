"""Bounded owner-session Supabase adapter, disabled unless explicitly enabled.

Only root project rows and private evidence objects are implemented. This is
not the hosted Store backend. Mock tests do not establish hosted RLS acceptance.
No JWT signature is verified locally: /auth/v1/user validates the session.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID

import httpx


class SupabaseError(ValueError):
    pass


class AccessDenied(SupabaseError):
    pass


class UncertainWrite(SupabaseError):
    """Caller must persist operation and reconcile it, never blindly replay."""

    def __init__(self, operation):
        super().__init__("Write outcome unknown; reconcile the stable operation identity")
        self.operation = operation


SAFE_WORKFLOW_ERRORS = frozenset({
    'Run incomplete, superseded or stale', 'Incomplete URL coverage',
    'Unresolved observations', 'Failed checks require explicit waiver or rejection',
    'Review reason required', 'Review version mismatch', 'Requirement revision conflict',
    'Worker ownership expired', 'Run not queued/current', 'Acceptance stale',
    'Current acceptance required', 'Selected dummy project unavailable',
})


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SupabaseError("Valid UUID required") from exc


class SupabaseAdapter:
    def __init__(self, *, url, project_ref, publishable_key, user_jwt, owner_id,
                 enabled=False, client=None):
        if enabled is not True:
            raise AccessDenied("Hosted adapter is disabled")
        if not isinstance(project_ref, str) or not re.fullmatch(r"[a-z0-9]{20}", project_ref):
            raise SupabaseError("Explicit supported project reference required")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != project_ref + ".supabase.co"
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise SupabaseError("Project URL does not match selected HTTPS Supabase reference")
        if not isinstance(publishable_key, str) or not publishable_key.startswith("sb_publishable_"):
            raise AccessDenied("Use a publishable key; privileged or legacy keys are unsupported")
        if not isinstance(user_jwt, str):
            raise AccessDenied("Authenticated application-user session required")
        try:
            parts = user_jwt.split(".")
            if len(parts) != 3 or not all(parts):
                raise ValueError("Malformed token")
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            if not isinstance(claims, dict) or type(claims.get("exp")) not in (int, float):
                raise ValueError("Invalid session claims")
            expires = float(claims["exp"])
            if not math.isfinite(expires):
                raise ValueError("Invalid session claims")
            if (claims.get("role") != "authenticated" or claims.get("iss") != url.rstrip("/") + "/auth/v1"
                    or expires <= time.time()):
                raise ValueError("Invalid session claims")
            self.owner_id = _uuid(owner_id)
            if _uuid(claims.get("sub")) != self.owner_id:
                raise ValueError("Owner mismatch")
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
            raise AccessDenied("Application-user token does not match the selected project/owner") from exc
        self.url = url.rstrip("/")
        self.project_ref = project_ref
        self._headers = {"apikey": publishable_key, "Authorization": "Bearer " + user_jwt}
        self._client = client or httpx.Client(timeout=15, follow_redirects=False)
        self._owns_client = client is None

    def close(self):
        if self._owns_client:
            self._client.close()

    def _request(self, method, path, *, operation=None, **kwargs):
        headers = dict(self._headers)
        headers.update(kwargs.pop("headers", {}))
        try:
            response = self._client.request(method, self.url + path, headers=headers,
                                            follow_redirects=False, timeout=15, **kwargs)
        except httpx.RequestError as exc:
            if operation:
                raise UncertainWrite(operation) from exc
            raise SupabaseError("Supabase request failed; details withheld to protect credentials") from exc
        if response.status_code in {401, 403}:
            raise AccessDenied("Supabase denied this application-user operation")
        if operation and (response.status_code >= 500 or response.status_code == 409):
            raise UncertainWrite(operation)
        if response.status_code not in range(200, 300):
            if response.status_code == 400:
                try:
                    error = response.json()
                except ValueError:
                    error = {}
                if (isinstance(error, dict) and error.get('code') == 'P0001'
                        and error.get('message') in SAFE_WORKFLOW_ERRORS):
                    raise SupabaseError(error['message'])
            raise SupabaseError("Supabase operation failed with HTTP " + str(response.status_code))
        return response

    def _json(self, response):
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise SupabaseError("Supabase returned malformed JSON") from exc

    def _owner(self):
        data = self._json(self._request("GET", "/auth/v1/user"))
        if not isinstance(data, dict) or data.get("id") != self.owner_id:
            raise AccessDenied("Supabase did not verify the expected application owner")

    def _row(self, data, row_id, name=None):
        if (not isinstance(data, dict) or data.get("id") != row_id
                or data.get("owner_id") != self.owner_id or data.get("mode") != "dummy"
                or not isinstance(data.get("name"), str)
                or (name is not None and data.get("name") != name)):
            raise SupabaseError("Returned root row identity/content mismatch")
        return data

    def read_root(self, row_id):
        row_id = _uuid(row_id)
        self._owner()
        data = self._json(self._request("GET", "/rest/v1/seo_qa_tool",
                                      params={"id": "eq." + row_id, "owner_id": "eq." + self.owner_id,
                                              "select": "id,owner_id,name,mode,created_at", "limit": "2"}))
        if not isinstance(data, list) or len(data) > 1:
            raise SupabaseError("Expected at most one root row")
        return self._row(data[0], row_id) if data else None

    def create_root(self, row_id, name="SEO QA Tool"):
        row_id = _uuid(row_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise SupabaseError("Bounded project name required")
        existing = self.read_root(row_id)
        if existing:
            return {"state": "replayed", "row": self._row(existing, row_id, name)}
        operation = {"kind": "create_root", "id": row_id, "owner_id": self.owner_id,
                     "name": name, "mode": "dummy"}
        response = self._request("POST", "/rest/v1/seo_qa_tool", operation=operation,
                                 headers={"Prefer": "return=representation"},
                                 json={k: operation[k] for k in ("id", "owner_id", "name", "mode")})
        try:
            data = self._json(response)
            if not isinstance(data, list) or len(data) != 1:
                raise SupabaseError("Expected one inserted row")
            row = self._row(data[0], row_id, name)
        except SupabaseError as exc:
            raise UncertainWrite(operation) from exc
        return {"state": "confirmed", "row": row}

    def reconcile_root(self, operation):
        if (operation.get("kind") != "create_root" or operation.get("owner_id") != self.owner_id
                or operation.get("mode") != "dummy"):
            raise AccessDenied("Operation is outside this owner/profile")
        row = self.read_root(operation.get("id"))
        if row is None:
            return {"state": "unknown", "reason": "No readable matching record; absence is not proof of failed write"}
        return {"state": "confirmed", "row": self._row(row, operation["id"], operation.get("name"))}

    def _object_path(self, path):
        if not isinstance(path, str) or len(path) > 500:
            raise SupabaseError("Bounded owner-relative object path required")
        parts = path.split("/")
        if (len(parts) < 2 or parts[0] != self.owner_id
                or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in {".", ".."} for p in parts)):
            raise AccessDenied("Object path must stay inside the verified owner's folder")
        return quote(path, safe="/")

    def upload_evidence(self, path, data, content_type="application/octet-stream"):
        path = self._object_path(path)
        if not isinstance(data, bytes) or not 0 < len(data) <= 2_000_000:
            raise SupabaseError("Evidence must be nonempty bytes up to 2 MB")
        if content_type not in {"application/json", "text/plain", "text/html", "text/csv", "image/png", "application/octet-stream"}:
            raise SupabaseError("Unsupported evidence content type")
        self._owner()
        operation = {"kind": "upload_evidence", "owner_id": self.owner_id, "path": path,
                     "sha256": hashlib.sha256(data).hexdigest()}
        self._request("POST", "/storage/v1/object/seo-evidence-private/" + path,
                      operation=operation, content=data,
                      headers={"content-type": content_type, "x-upsert": "false"})
        try:
            return self.reconcile_evidence(operation)
        except SupabaseError as exc:
            # The upload was dispatched. A failed readback cannot establish that
            # it failed; preserve identity so callers reconcile without a POST.
            raise UncertainWrite(operation) from exc

    def reconcile_evidence(self, operation):
        if operation.get("kind") != "upload_evidence" or operation.get("owner_id") != self.owner_id:
            raise AccessDenied("Evidence operation owner mismatch")
        path = self._object_path(operation.get("path"))
        self._owner()
        # Authenticated download verifies actual bytes; never use public URL.
        response = self._request("GET", "/storage/v1/object/seo-evidence-private/" + path)
        if hashlib.sha256(response.content).hexdigest() != operation.get("sha256"):
            raise SupabaseError("Evidence byte readback mismatch")
        return {"state": "confirmed", "path": path, "sha256": operation["sha256"]}

    def signed_evidence_url(self, path, expires_in=60):
        path = self._object_path(path)
        if type(expires_in) is not int or not 1 <= expires_in <= 300:
            raise SupabaseError("Signed access lifetime must be 1-300 seconds")
        self._owner()
        data = self._json(self._request("POST", "/storage/v1/object/sign/seo-evidence-private/" + path,
                                       json={"expiresIn": expires_in}))
        signed = data.get("signedURL") if isinstance(data, dict) else None
        if not isinstance(signed, str):
            raise SupabaseError("Signed access URL missing")
        if signed.startswith("/object/sign/"):
            signed = self.url + "/storage/v1" + signed
        parsed = urlsplit(signed)
        expected_path = "/storage/v1/object/sign/seo-evidence-private/" + path
        if (parsed.scheme != "https" or parsed.netloc != urlsplit(self.url).netloc
                or parsed.path != expected_path or not parse_qs(parsed.query).get("token") or parsed.fragment):
            raise SupabaseError("Signed access URL points outside the expected project/object")
        return {"url": signed, "expires_in": expires_in, "sensitive": True}
