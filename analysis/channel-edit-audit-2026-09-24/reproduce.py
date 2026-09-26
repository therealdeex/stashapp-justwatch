"""Read-only audit of production code; all writes use disposable directories."""
import copy
import json
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from justwatch import channel_ops, criteria, library, refresh, snapshots
fixture = runpy.run_path(str(ROOT / 'tests/test_channel_library.py'))['base_library']
results = {}

class MissingClient:
    def submit(self, query, variables=None):
        return {'findTag': None, 'findStudio': None, 'findPerformer': None}

for case in ('duration', 'empty_facets', 'missing_id', 'unknown_field', 'zero_max', 'missing_exclusion', 'missing_positive'):
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)
        doc=fixture()
        doc['channels'][0]['source']={'type':'criteria','tags':['5'],'duration':{'min':3600}}
        library.save(p,doc)
        channel=copy.deepcopy(doc['channels'][0])
        channel['source']['duration']['min']=4800
        if case=='empty_facets': channel['source'].update(tags=[],tagsAny=[])
        if case=='missing_id':
            channel['id']='ch_deadbeef'; channel['number']=99
        if case=='unknown_field': channel['unexpected']='extra'
        if case=='zero_max': channel['source']={'type':'criteria','tags':['5'],'duration':{'max':0}}
        if case=='missing_exclusion': channel['source']['excludeTags']=['99999999']
        if case=='missing_positive': channel['source']['tags']=['99999999']
        ops=[{'op':'channel.put','channel':channel}]
        ctx=SimpleNamespace(data_dir=p,assets_dir=p/'assets',client=MissingClient(),args={'ops':ops,'expectedRevision':1,'requestId':case})
        try: validation=channel_ops.op_validate_channel_changes(ctx)
        except Exception as exc: validation={'exception':type(exc).__name__,'message':str(exc)}
        try: receipt=channel_ops.op_apply_channel_changes(ctx)
        except Exception as exc: receipt={'exception':type(exc).__name__,'message':str(exc)}
        results[case]={'validation':validation,'apply':receipt,'durableReceipt':library.get_receipt(p,case)}
        if case=='zero_max':
            results[case]['draftFilter']=criteria.build_scene_filter(channel['source'])
            results[case]['storedFilter']=criteria.build_scene_filter(library.load(p)['channels'][0]['source'])

for case in ('unavailable_health', 'sort_health_skip', 'stale_sort_worker'):
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp); doc=fixture(); library.save(p,doc)
        c=doc['channels'][0]; cid=c['id']; sig=criteria.source_signature(c['source'])
        health={'healthStatus':'ok','sourceSignature':sig,'sceneCount':1,'loopSeconds':600,'rotationVersion':'OLD'}
        if case=='sort_health_skip':
            snapshots.write_snapshot(p,p/'assets',{'revision':1,'channels':{cid:health}})
            c['sort']='newest'; library.save(p,doc)
        refresh.enqueue(p,[cid],1,{cid:sig})
        def compute(*args):
            if case=='unavailable_health': return {'healthStatus':'unavailable'}
            latest=library.load(p); latest['channels'][0]['sort']='newest'; latest['revision']+=1; library.save(p,latest)
            return health
        with patch.object(snapshots,'_channel_health',side_effect=compute) as probe:
            result=refresh.process_pending(MissingClient(),p,p/'assets')
        results[case]={'result':result,'computeCalls':probe.call_count,'pending':refresh.read_pending(p),'health':snapshots.read_snapshot(p,p/'assets').get('channels',{}).get(cid)}

with tempfile.TemporaryDirectory() as tmp:
    doc=fixture(); c=doc['channels'][0]
    c['source']={'type':'criteria','duration':{'min':4800}}
    with patch.object(snapshots.lineup,'fetch_rotation',return_value={
        'items':[],'sourceTotal':0,'rotationComplete':True,'loopSeconds':0,'rotationVersion':'empty'}):
        results['empty_rule_pool']=snapshots._channel_health(MissingClient(),c,{})

browser_file=Path(__file__).with_name('browser-results.json')
if browser_file.exists():
    browser=json.loads(browser_file.read_text())
    source=browser['remove_tag_then_duration']['source']
    results['browser_payload_server_validation']={'source':source,'errors':criteria.validate(source)}

out=Path(__file__).with_name('results.json')
out.write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps(results,indent=2))
