"""One canonical assessment, four offline representations."""
import csv
from html import escape
import io
import json
from pathlib import Path
import re

from .profile import digest


def to_json(report):
    return json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n'


def to_csv(report):
    # JSON payloads start with { or [, preventing spreadsheet formula execution.
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['record_type', 'payload_json'])
    writer.writerow(['library', json.dumps({k: v for k, v in report.items() if k != 'pages'}, ensure_ascii=False)])
    for page in report['pages']:
        writer.writerow(['page', json.dumps(page, ensure_ascii=False)])
    return out.getvalue()


def to_markdown(report):
    payload = to_json(report)
    length = max([len(m[0]) for m in re.finditer(r'`+', payload)] or [2]) + 1
    fence = '`' * max(3, length)
    return '# AI content quotability — local adaptation\n\nNot a citation probability or human acceptance.\n\n' + fence + 'json\n' + payload + fence + '\n'


def to_html(report):
    payload = to_json(report).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    title = escape(report['profile']['label'])
    return '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI content quotability — local report</title>
<style>
:root{font:16px system-ui;color:#16302e;background:#f3f6f3}body{margin:0}main{max-width:1200px;margin:auto;padding:28px}h1{font-size:clamp(1.6rem,4vw,2.5rem)}p{max-width:85ch;line-height:1.6;overflow-wrap:anywhere}button,input,select{font:inherit;padding:9px;border:1px solid #647976;border-radius:5px;background:white;color:#16302e}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #a6601e}label{display:inline-block;margin:8px}table{border-collapse:collapse;width:100%;background:white}th,td{text-align:left;border-bottom:1px solid #d3dfda;padding:12px}th button{border:0;font-weight:bold}caption{text-align:left;margin:14px 0;font-weight:bold}.scroll{overflow:auto}section,details{margin:22px 0;padding:18px;background:white;border:1px solid #d3dfda;border-radius:10px}summary{cursor:pointer;font-weight:bold}.cell{font-size:.85rem;min-width:80px}blockquote{border-left:3px solid #91aaa0;padding:12px;margin-left:0;white-space:pre-wrap}.muted{color:#526762}.unknown{background:#eee}code{overflow-wrap:anywhere}a{color:#185c4e}.toolbar{display:flex;flex-wrap:wrap;gap:8px}.badge{font-weight:bold}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style><main><a href="#pages">Skip to articles</a><h1>''' + title + '''</h1>
<p>This report assesses supplied content. It does not predict citations, verify expertise or change existing SEO scores and acceptance. Semantic judgments and substantive updates need retained reviewer evidence.</p>
<p id="scope" role="status"></p><p id="version" class="muted"></p>
<div class="toolbar"><button id="json">Download JSON</button><button id="csv">Download CSV</button><button id="markdown">Download Markdown</button></div>
<section><h2>Library comparison</h2><label>Find article <input id="filter" type="search"></label><label>Sort by <select id="sort"><option value="title">Article title</option><option value="lower">Earned lower bound</option><option value="coverage">Evidence coverage</option></select></label>
<div class="scroll" tabindex="0" role="region" aria-label="Scrollable criterion comparison"><table id="pages"><caption>Articles and criterion heatmap. Unknown cells earn no assumed credit.</caption><thead id="head"></thead><tbody id="body"></tbody></table></div><p id="count" aria-live="polite"></p></section>
<section><h2>Recurring editorial patterns</h2><div id="patterns"></div></section>
<section><h2>Sector benchmarks and actual citations</h2><p>Sector benchmarks: unavailable. Actual AI citations: unmeasured. Sample coverage is not a full-site audit.</p></section>
<h2>Section evidence and feedback</h2><div id="details"></div>
<details><summary>Methodology and evidence history</summary><pre id="method"></pre></details>
<noscript><p>Use the adjacent report.json, report.csv or report.md to view the complete retained assessment without JavaScript.</p></noscript>
<script type="application/json" id="data">''' + payload + '''</script>
<script>
const report=JSON.parse(document.getElementById('data').textContent);
function el(tag,text,parent){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(parent)parent.append(n);return n;}
const c=report.coverage;
document.getElementById('scope').textContent=`${report.pages.length} articles · ${report.status} · ${c.sampled?'sampled':c.scope} · missing ${c.missing_ids.length}, unexpected ${c.unexpected_ids.length}`;
document.getElementById('version').textContent=`${report.profile.version} · assessed ${report.evaluated_at} · profile ${report.profile.sha256}`;
document.getElementById('method').textContent=JSON.stringify({profile:report.profile,coverage:c},null,2);
const hr=el('tr',undefined,document.getElementById('head'));
['Article','Assessment','Evidence',...report.profile.rules.map(r=>r.id.replaceAll('_',' '))].forEach(t=>el('th',t,hr));
function draw(){const body=document.getElementById('body');body.replaceChildren();const q=document.getElementById('filter').value.toLowerCase();const sort=document.getElementById('sort').value;const pages=report.pages.filter(p=>(p.source.title+' '+p.source.id).toLowerCase().includes(q)).slice().sort((a,b)=>sort==='title'?a.source.title.localeCompare(b.source.title):sort==='coverage'?b.evidence_coverage_weight-a.evidence_coverage_weight:b.bounds.lower-a.bounds.lower);
for(const p of pages){const tr=el('tr',undefined,body);el('td',p.source.title,tr);el('td',p.score===null?`${p.bounds.lower}–${p.bounds.upper} / 100 (incomplete)`:`${p.score} / 100`,tr);el('td',`${p.evidence_coverage_weight}% · ${p.source.evidence_kind}`,tr);for(const r of p.criteria){const td=el('td',r.points===null?'Unmeasured':`${r.points}/${r.weight}`,tr);td.className='cell';if(r.fraction===null)td.classList.add('unknown');else td.style.backgroundColor=`hsl(155 30% ${96-r.fraction*40}%)`;td.title=`${r.id}: ${r.state}`;}}
document.getElementById('count').textContent=`Showing ${pages.length} of ${report.pages.length} articles`;}
document.getElementById('filter').addEventListener('input',draw);document.getElementById('sort').addEventListener('change',draw);draw();
const patterns=document.getElementById('patterns');if(!Object.keys(report.patterns).length)el('p','No configured editorial patterns detected. This does not establish semantic quality.',patterns);
for(const [name,p] of Object.entries(report.patterns))el('p',`${name.replaceAll('_',' ')}: ${p.occurrences} sections across ${p.page_ids.length} articles`,patterns);
for(const p of report.pages){const d=el('details',undefined,document.getElementById('details'));el('summary',p.source.title,d);el('p',`Evidence: ${p.source.evidence_kind} · source ${p.source.content_sha256} · review ${p.review_sha256}`,d);el('p',`Updated-date evidence, named-source claims and semantic judgments are reviewer-supplied. Status: ${p.status}.`,d);for(const s of p.sections){el('h3',s.heading,d);el('p',`${s.id} · ${s.opening_kind||'no opening'} · ${s.opening_locator||s.heading_locator}`,d);el('blockquote',s.opening===null?'No opening retained':s.opening,d);for(const [key,rows] of Object.entries(p.semantic_decisions)){const decision=rows.find(v=>v.section_id===s.id);el('p',`${key.replaceAll('_',' ')}: ${decision.state}${decision.value===null?'':decision.value?' · yes':' · no'}`,d);}for(const f of p.feedback.filter(f=>f.section_id===s.id))el('p',`${f.kind}: ${f.action}`,d);}const evidence=el('details',undefined,d);el('summary','Retained measurements and review history',evidence);el('pre',JSON.stringify({categories:p.categories,criteria:p.criteria,review:p.review,watch_metrics:p.watch_metrics},null,2),evidence);}
function download(name,text,type){const url=URL.createObjectURL(new Blob([text],{type}));const a=el('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
document.getElementById('json').onclick=()=>download('report.json',JSON.stringify(report,null,2)+'\\n','application/json');
document.getElementById('csv').onclick=()=>{const quote=s=>'"'+s.replaceAll('"','""')+'"';const {pages,...meta}=report;const rows=[['record_type','payload_json'],['library',JSON.stringify(meta)],...pages.map(p=>['page',JSON.stringify(p)])];download('report.csv',rows.map(r=>r.map(quote).join(',')).join('\\r\\n')+'\\r\\n','text/csv');};
document.getElementById('markdown').onclick=()=>{const text=JSON.stringify(report,null,2);const runs=text.match(/`+/g)||['``'];const fence='`'.repeat(Math.max(3,...runs.map(s=>s.length+1)));download('report.md','# AI content quotability — local adaptation\\n\\nNot a citation probability or human acceptance.\\n\\n'+fence+'json\\n'+text+'\\n'+fence+'\\n','text/markdown');};
</script></main></html>'''


def write_bundle(report, destination):
    path = Path(destination)
    path.mkdir(parents=True, exist_ok=True)
    # Keep every distinct assessment and methodology, even when refreshing a desk.
    history = path/'history'
    history.mkdir(exist_ok=True)
    snapshot = history/(digest(report)+'.json')
    payload = to_json(report)
    if snapshot.exists():
        if snapshot.read_text(encoding='utf-8') != payload:
            raise ValueError('Existing history snapshot failed its identity check')
    else:
        with snapshot.open('x', encoding='utf-8') as stream:
            stream.write(payload)
    for name, text in [('report.json', payload), ('report.csv', to_csv(report)),
                       ('report.md', to_markdown(report)), ('report.html', to_html(report))]:
        (path/name).write_text(text, encoding='utf-8')
    return str((path/'report.html').resolve())
