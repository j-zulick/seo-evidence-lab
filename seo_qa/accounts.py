"""Explicit multi-account allowlist for shared browser sign-in.

The configured owner is always allowed. Additional accounts are allowed only
when listed in SEO_MEMBER_ACCOUNT_IDS as canonical UUIDs. Nothing here widens
access implicitly: an unset or empty member list keeps single-owner behavior,
and any malformed, duplicate or oversized list fails closed before any I/O.

Each account keeps its own storage root (derived from its own UUID) and its own
session rows. Accounts never share records through this policy; shared client
workspaces remain governed by client_workspace memberships.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from uuid import UUID

MAX_MEMBER_ACCOUNTS = 25
MEMBERS_ENV = 'SEO_MEMBER_ACCOUNT_IDS'


class AccountPolicyError(ValueError):
    """Configuration errors never echo the rejected value."""


def canonical_uuid(value):
    try:
        parsed = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise AccountPolicyError('Account identities must be canonical UUIDs') from None
    if parsed != value:
        raise AccountPolicyError('Account identities must be canonical UUIDs')
    return parsed


@dataclass(frozen=True)
class AccountPolicy:
    owner_id: str
    member_ids: tuple[str, ...] = ()

    def __post_init__(self):
        canonical_uuid(self.owner_id)
        if not isinstance(self.member_ids, tuple):
            raise AccountPolicyError('Member accounts must be an explicit tuple')
        if len(self.member_ids) > MAX_MEMBER_ACCOUNTS:
            raise AccountPolicyError('Too many member accounts configured')
        seen = {self.owner_id}
        for member in self.member_ids:
            canonical_uuid(member)
            if member in seen:
                raise AccountPolicyError('Member accounts must be unique and exclude the owner')
            seen.add(member)

    @property
    def account_ids(self):
        return frozenset((self.owner_id,) + self.member_ids)

    @property
    def multi_account(self):
        return bool(self.member_ids)

    def allows(self, account_id):
        return isinstance(account_id, str) and account_id in self.account_ids

    def require(self, account_id):
        if not self.allows(account_id):
            raise AccountPolicyError('Account is not permitted for this deployment')
        return account_id

    @classmethod
    def from_environ(cls, environ=None):
        values = os.environ if environ is None else environ
        owner = canonical_uuid(values.get('SEO_SUPABASE_OWNER_ID', ''))
        raw = values.get(MEMBERS_ENV, '')
        if not isinstance(raw, str):
            raise AccountPolicyError('Member account list is invalid')
        if not raw.strip():
            return cls(owner)
        parts = raw.split(',')
        if any(not part.strip() for part in parts):
            raise AccountPolicyError('Member account list contains an empty entry')
        return cls(owner, tuple(canonical_uuid(part.strip()) for part in parts))


def token_subject(token):
    """Unverified JWT subject used only to select which identity to verify.

    Callers must still confirm the subject with the Auth server (SupabaseAdapter
    requires the user endpoint to return the same id) before trusting it.
    """
    try:
        encoded = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        return canonical_uuid(claims['sub'])
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, UnicodeError):
        raise AccountPolicyError('Token does not name a permitted account') from None
