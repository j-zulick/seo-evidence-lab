"""Explicit offline fixtures; no website fetching and no real business records."""
from .inspector import inspect_html

FIXTURE_URL='https://fixture.example/category/coffee-tables'
GOOD_HTML='''<!doctype html><html><head><title>Dummy coffee tables</title>
<link rel="canonical" href="https://fixture.example/category/coffee-tables">
<meta name="robots" content="index,follow"></head><body><h1>Dummy coffee tables</h1>
<p>Width: 80 cm. Shipping: 5 days. Fixture data only.</p></body></html>'''
BAD_HTML=GOOD_HTML.replace('https://fixture.example/category/coffee-tables','https://fixture.example/old-category').replace('index,follow','noindex,follow')


def fixture_inspector(url,expected):
    if url not in (FIXTURE_URL,FIXTURE_URL+'?variant=broken',FIXTURE_URL+'?variant=blocked'):
        raise ValueError('Local dummy inspector only accepts seeded fixture URLs; live inspection is separate')
    if url.endswith('variant=blocked'):
        return inspect_html('Access denied',url,status=403,expected=expected)
    html=BAD_HTML if 'variant=broken' in url else GOOD_HTML
    result=inspect_html(html,url,robots_txt='User-agent: *\nAllow: /',expected=expected)
    result['data_mode']='dummy'
    return result


def seed(store):
    if store.list_requirements():
        return store.list_requirements()
    expected={'canonical':FIXTURE_URL,'indexable':True,'crawlable':True,'allowed_statuses':[200],'required_text':['Width: 80 cm']}
    return [store.create_requirement('Dummy category release — correct',[FIXTURE_URL],expected),
            store.create_requirement('Dummy category release — broken',[FIXTURE_URL+'?variant=broken'],expected),
            store.create_requirement('Dummy category release — blocked',[FIXTURE_URL+'?variant=blocked'],expected)]
