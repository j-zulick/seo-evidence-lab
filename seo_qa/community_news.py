"""Offline subreddit lead review. All bundled observations are synthetic."""
import json
from datetime import datetime
from urllib.parse import urlsplit

from .competitor import ROOT, digest


def review(payload):
    if not isinstance(payload, dict) or len(json.dumps(payload).encode()) > 500_000:
        raise ValueError('Community input exceeds limit')
    posts = payload.get('posts')
    if not isinstance(posts, list) or len(posts) > 100:
        raise ValueError('Expected at most 100 posts')
    seen = set()
    leads = []
    for post in posts:
        key = post['id']
        if not isinstance(key, str) or not key or len(key) > 100:
            raise ValueError('Invalid post identity')
        if key in seen:
            raise ValueError('Duplicate post identity')
        seen.add(key)
        removed = post.get('removed', False)
        if type(removed) is not bool:
            raise ValueError('Expected boolean removal status')
        if removed:
            leads.append({'id': key, 'status': 'removed', 'text_retained': False})
            continue
        title = post['title']
        if not isinstance(title, str) or len(title) > 500:
            raise ValueError('Invalid lead title')
        created, observed = (datetime.fromisoformat(post[k]) for k in ('created_at','observed_at'))
        if created.tzinfo is None or observed.tzinfo is None or observed < created:
            raise ValueError('Expected ordered timezone-aware dates')
        links = post.get('primary_sources', [])
        if not isinstance(links, list) or len(links) > 10:
            raise ValueError('Too many source links')
        for link in links:
            parsed = urlsplit(link)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Expected public HTTPS source locator')
        # A supplied link is a review lead. Its accuracy/authority still requires
        # inspection; social engagement never promotes a lead into guidance.
        leads.append({'id':key, 'subreddit':post['subreddit'], 'title':title,
                      'created_at':created.isoformat(), 'observed_at':observed.isoformat(),
                      'primary_sources':links, 'status':'primary review needed' if links else 'unverified lead',
                      'content_hash':digest(title), 'guidance_approved':False})
    return {'schema':'community-leads/v1', 'synthetic':payload.get('synthetic') is True,
            'leads':leads, 'publication':'human review pending', 'network_calls':0}


def fixture():
    return review(json.loads((ROOT/'fixtures/community-news.json').read_text()))
