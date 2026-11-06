"""Request-local identity/storage; no browser session lives in process globals."""
from contextvars import ContextVar

active_store = ContextVar('seo_active_store', default=None)
active_csrf = ContextVar('seo_active_csrf', default=None)


class RequestStore:
    """Delegate only within an authenticated request's ContextVar scope."""
    def __getattr__(self, name):
        store = active_store.get()
        if store is None:
            raise ValueError('An authenticated request is required')
        return getattr(store, name)


def request_csrf(fallback):
    value = active_csrf.get()
    return value if value is not None else fallback
