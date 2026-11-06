"""Create an allowlisted runtime directory/ZIP without copying local state."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vercel_build import RUNTIME_FILES, check


def build(source, destination):
    source, destination = Path(source), Path(destination)
    check(source, installed=False)
    payloads = {name:(source/name).read_bytes() for name in sorted(RUNTIME_FILES)}
    check(source, installed=False)
    if any((source/name).read_bytes()!=body for name,body in payloads.items()):
        raise ValueError('Runtime source changed during packaging')
    # Create-only; never delete or overwrite another candidate/build directory.
    destination.mkdir(parents=True, exist_ok=False)
    runtime = destination/'source'; runtime.mkdir()
    rows = []
    for name, body in payloads.items():
        path = runtime/name; path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
        rows.append({'path':name,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()})
    archive = destination/'vercel-source.zip'
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED) as out:
        for name, body in payloads.items():
            info=zipfile.ZipInfo(name,date_time=(2026,11,4,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16;out.writestr(info,body)
    with zipfile.ZipFile(archive) as out:
        if out.testzip() or out.namelist()!=list(payloads):
            raise ValueError('Archive inventory/integrity failure')
        if any(out.read(name)!=body for name,body in payloads.items()):
            raise ValueError('Archived bytes changed')
    result={'schema':'seo-vercel-source/v1','files':rows,
            'tree_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
            'archive_readback_verified':True,'source':str(source.resolve()),
            'runtime_source':str(runtime.resolve()),'archive':str(archive.resolve()),
            'scope':'Source package; no platform build/deployment implied',
            'excluded':['credentials','state','sessions','private evidence','tests','fixtures',
                        'knowledge databases','schedules','browsers','development dependencies','Git metadata']}
    (destination/'MANIFEST.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=build(args.source,args.output)
    print(json.dumps({'files':len(result['files']),'archive_sha256':result['archive_sha256']}))
