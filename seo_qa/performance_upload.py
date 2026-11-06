"""X06 upload consumer. Inject the published import_upload callable."""
import csv
import io
import json
from .performance_analysis import analyze, FIELDS

def analyze_upload(payload, importer, format='json', metadata=None):
    imported = importer(payload, format, metadata)
    if imported.get('contract') != 'crawl-observations/v1' or not imported.get('valid'):
        raise ValueError('Valid crawl-observations/v1 import required: '+str(imported.get('errors')))
    pages=[]
    for page in imported['rows']:
        observations=page.get('observations',{})
        raw, rendered=observations.get('raw'),observations.get('rendered')
        paired=bool(raw and rendered and all(raw.get(k)==rendered.get(k) for k in ('source','capture_id','captured_at','device')))
        rendering={f:'missing' if not paired or f not in raw or f not in rendered else 'same' if raw[f]==rendered[f] else 'changed' for f in FIELDS}
        measurements=[]
        for obs in page.get('performance',[]):
            row=dict(obs,url=page['identity'],synthetic=imported['metadata'].get('source_type')=='synthetic')
            measurements.append(analyze({'schema':'x08-observations/v1','observations':[row]})['results'][0])
        pages.append({'url':page['identity'],'performance':measurements,'performance_availability':'observed' if measurements else 'missing','rendering':rendering,'paired':paired,'rendering_provenance':observations})
    return {'schema':'x08-upload-report/v1','source_hash':imported['source_hash'],'metadata':imported['metadata'],'pages':pages,'live_collection':'unsupported','pairing_policy':'same page identity, source, capture_id and exact timestamp; device unknown unless supplied','limitations':['Uploader assertions only. No field CWV overall verdict. No cross-device/window aggregation.']}

def exports(report):
    stream=io.StringIO(newline=''); writer=csv.writer(stream)
    writer.writerow(['url','paired','performance_availability','rendering_json','performance_json'])
    for p in report['pages']:
        cells=[p['url'],str(p['paired']),p['performance_availability'],json.dumps(p['rendering'],sort_keys=True),json.dumps(p['performance'],sort_keys=True)]
        writer.writerow(["'"+v if v.startswith(('=','+','-','@','\t','\r')) else v for v in cells])
    return {'json':json.dumps(report,sort_keys=True,allow_nan=False),'csv':stream.getvalue(),'markdown':'# Uploaded performance/rendering report\n\nSource SHA256: '+report['source_hash']+'\n\n```json\n'+json.dumps(report,sort_keys=True).replace('`','\\u0060')+'\n```\n'}
