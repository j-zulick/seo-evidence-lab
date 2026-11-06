"""Account-free check that a local successor cannot produce a hosted package."""
from pathlib import Path
import sys,tempfile,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.package_vercel import build
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='seo-boundary-') as temp:
 destination=Path(temp)/'must-not-exist'
 try: build(root,destination)
 except ValueError as exc:
  assert 'Local-only integrated release' in str(exc),str(exc)
 else: raise AssertionError('Local-only source unexpectedly packaged')
 assert not destination.exists()
 print(json.dumps({'local_only_packaging_refused':True,'output_created':False,'network_calls':0}))
