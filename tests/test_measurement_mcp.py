import asyncio
import json
import sys
from pathlib import Path
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from seo_qa.measurement import fixture_view, gsc, crux, comparison_allowed
from seo_qa.timeline_eval import run


def test_measurement():
    result=fixture_view()
    assert result['gsc']['rows'][0]['clicks']==14
    assert result['crux']['scope']=='origin'
    assert result['crux']['p75']['interaction_to_next_paint'] is None
    assert crux({'error':{'status':'NOT_FOUND'}})['coverage']=='unavailable'
    with pytest.raises(ValueError):
        gsc({'request':{'dimensions':['query']},'response':{}})
    row=dict(source='CrUX',scope='url',target='a',metric='LCP',unit='ms',value=100,period=['2026-10-01','2026-10-07'])
    nextrow=row|{'period':['2026-10-08','2026-10-14']}
    assert comparison_allowed(row,nextrow)
    for change in [{'scope':'origin'},{'source':'GSC'},{'period':row['period']},{'value':None},{'device':'PHONE'}]:
        assert not comparison_allowed(row,nextrow|change)


def test_eval(tmp_path):
    report=run(tmp_path)
    rows=report['cases']
    assert sum(r['baseline_pass'] for r in rows)==4
    assert all(r['revised_pass'] for r in rows)
    assert report['human_labels'] is None
    assert len(json.loads((tmp_path/'langfuse-dataset-items.json').read_text()))==6


def test_stdio_roundtrip():
    async def exercise():
        params=StdioServerParameters(command=sys.executable,args=['-m','seo_qa.timeline_mcp'],cwd=str(Path(__file__).resolve().parents[1]))
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                tools=(await session.list_tools()).tools
                assert len(tools)==3 and all(tool.annotations.readOnlyHint for tool in tools)
                async def call(name,args):
                    res=await session.call_tool(name,args)
                    assert not res.isError
                    return json.loads(res.content[0].text)
                ids=(await call('get_competitor_timeline',{}))['audit_ids']
                assert (await call('get_finding_evidence',{'audit_id':ids[0]}))['finding']=='miss'
                assert (await call('compare_audit_versions',{'before_id':ids[0],'after_id':ids[1]}))['status']=='explained'
                assert (await session.call_tool('get_finding_evidence',{'audit_id':'unknown'})).isError
    asyncio.run(exercise())
