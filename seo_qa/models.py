from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')


class RequirementInput(Record):
    title: str = Field(min_length=1, max_length=200)
    urls: list[str] = Field(min_length=1, max_length=100)
    expected: dict[str, Any]

    @field_validator('urls')
    @classmethod
    def unique_urls(cls, value):
        from urllib.parse import urlsplit
        if len(set(value)) != len(value):
            raise ValueError('Duplicate URLs')
        if any(urlsplit(u).scheme not in ('http', 'https') or not urlsplit(u).hostname or urlsplit(u).username for u in value):
            raise ValueError('Only explicit HTTP(S) URLs without credentials')
        return value

    @field_validator('expected')
    @classmethod
    def known_checks(cls, value):
        types = {'canonical': str, 'indexable': bool, 'crawlable': bool, 'redirect_final': str,
                 'max_redirects': int, 'allowed_statuses': list, 'crawler': str, 'required_text': list,
                 'seo_qa': dict}
        if not value or not (set(value) - {'crawler'}):
            raise ValueError('At least one expectation is required')
        for key, item in value.items():
            if key not in types or type(item) is not types[key]:
                raise ValueError('Unsupported expectation or type: ' + key)
        if 'max_redirects' in value and not 0 <= value['max_redirects'] <= 10:
            raise ValueError('Redirect bound must be 0–10')
        if 'allowed_statuses' in value and (not value['allowed_statuses'] or any(type(x) is not int or x < 100 or x > 599 for x in value['allowed_statuses'])):
            raise ValueError('Invalid statuses')
        if 'required_text' in value and (not value['required_text'] or any(not isinstance(x,str) or not x.strip() for x in value['required_text'])):
            raise ValueError('Invalid required text')
        if 'seo_qa' in value:
            from .scope_criteria import validate_config
            if set(value) != {'seo_qa'}:
                raise ValueError('General scope input cannot be mixed with legacy expectations')
            validate_config(value['seo_qa'])
        return value


class Finding(Record):
    check: str
    outcome: Literal['pass', 'fail', 'blocked', 'inconclusive', 'not_applicable']
    expected: Any = None
    observed: Any = None
    reason: str


class Observation(Record):
    model_config = ConfigDict(extra='allow')
    url: str
    final_url: str | None = None
    status: int | None = None
    observed_at: str
    evidence: dict[str, Any]
    findings: list[Finding]
    limitations: list[str] = Field(default_factory=list)
