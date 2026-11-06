"""Minimized workspace/explanation downloads; caller owns authenticated lookup."""
import csv,hashlib,io,json
from fastapi.responses import Response
from .csv_export import canonical,safe_cell,scrub
from .markdown_export import _text
NOTICE='JSON is authoritative. Raw input/capture fields, headers, blobs and full knowledge graph are omitted; free text may remain private.'
def projection(document):
    omitted=[]
    def visit(value,path=''):
        if isinstance(value,dict):
            out={}
            for key,child in value.items():
                private=key in {'input','raw_input','raw','rendered','html','capture_html','headers','evidence_blobs','knowledge_graph'} or key=='content' and path.endswith('/seo_qa')
                if (path.endswith('/knowledge') or path == '/uncertainty') and key in {'blobs','records','freshness_evidence'}:
                    private=True
                if private:omitted.append(path+'/'+key)
                else:out[key]=visit(child,path+'/'+key)
            return out
        if isinstance(value,list):return [visit(v,path+'/'+str(i)) for i,v in enumerate(value)]
        return value
    return {'schema':'workspace-report-projection/v1','report_ref':'sha256:'+hashlib.sha256(canonical(document).encode()).hexdigest(),'facts':scrub(visit(document)),'omitted_paths':omitted,'notice':NOTICE}
def export(document,format):
    value=projection(document)
    if format=='csv':
        stream=io.StringIO(newline='');writer=csv.writer(stream,quoting=csv.QUOTE_ALL,lineterminator='\r\n')
        writer.writerow(['schema_version','report_ref','payload_json'])
        writer.writerow([safe_cell(value['schema']),safe_cell(value['report_ref']),safe_cell(canonical(value))])
        return stream.getvalue().encode('utf-8-sig')
    if format=='md':return ('# Workspace report\n\n'+NOTICE+'\n\n'+_text(canonical(value))+'\n').encode()
    raise ValueError('Unsupported minimized export format')
def download(document,format,name):
    return Response(export(document,format),media_type={'csv':'text/csv','md':'text/markdown'}[format],headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Disposition':f'attachment; filename="{name}.{format}"'})
