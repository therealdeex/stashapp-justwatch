#!/usr/bin/env python3
"""Read-only product audit: synthetic fixtures and temporary directories only.
Run from any cwd: python3 analysis/channel-curation-audit/reproduce.py
No live Stash, device, rollout, or application files are changed.
"""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from justwatch import library, channel_ops, main, criteria, lineup, programming, continuing, refresh, snapshots
from tools import migrate_channel_library as migration


def fixture():
    return {
        'schemaVersion': 1, 'libraryId': 'lib_audit', 'revision': 1,
        'groups': [{'id': 'grp_my', 'name': 'My Channels', 'position': 1, 'legacySection': None},
                   {'id': 'grp_general', 'name': 'General', 'position': 2, 'legacySection': 'general'}],
        'channels': [dict(id=f'ch_{i:08x}', kind='ch', number=i, name=f'Channel {i}',
                          glyph=None, color='#112233', groupId='grp_my', sort='shuffle', seed=i,
                          enabled=True, archived=False, paused=False, source={'type':'tag','id':'5','ids':['5']},
                          sourceLabel='Tag 5', programming={'mode':'fixed'}, provenance={'origin':'custom'})
                     for i in (1, 2)], 'settings': {}, 'recentRequests': []}


class FakeClient:
    def __init__(self, playable=True):
        self.calls=[]; self.playable=playable
    def submit(self, query, variables):
        self.calls.append(copy.deepcopy(variables))
        if 'findScenes' in query:
            return {'findScenes': {'count': 100, 'scenes': [
                {'id':'scene-old','title':'Synthetic','studio':{'name':'S'},
                 'files':[{'duration':600}] if self.playable else [], 'paths':{'preview':'/mock.jpg'}}]}}
        return {'findTag': {'name': 'Tag'}, 'findPerformer': {'name': 'Performer'}, 'findStudio': {'name': 'Studio'}}


def ctx(p, args=None, client=None):
    return SimpleNamespace(data_dir=p, assets_dir=p/'assets', args=args or {}, client=client or FakeClient())


def capture(fn):
    try:
        return {'result': fn()}
    except Exception as e:
        return {'exception':type(e).__name__, 'message':str(e)}


def new_op(temp, number=500):
    return {'op':'channel.create','tempId':temp,'channel':{'kind':'net','number':number,'name':temp,
            'groupId':'grp_general','source':{'type':'tag','id':'5'}}}


def rules_playback(p):
    d=fixture(); d['channels'][0]['source']={'type':'criteria','tagsAny':['5']}; library.save(p,d)
    c=ctx(p,{'source':d['channels'][0]['source'],'channelId':d['channels'][0]['id']})
    return {'preview':capture(lambda:channel_ops.op_preview_channel_pool(c)),
            'lineup':capture(lambda:main._op_lineup(c)),
            'custom_index':capture(lambda:programming.index_source(c.client,d['channels'][0])),
            'continuing_index':capture(lambda:continuing.index_source(c.client,d['channels'][0])),
            'distinct_sources_same_legacy_key':lineup.source_key({'type':'criteria','tagsAny':['5']}) == lineup.source_key({'type':'criteria','tagsAny':['6']})}


def patch_identity(p):
    library.save(p,fixture())
    r=library.apply_transaction(p,expected_revision=1,request_id='patch-id',ops=[
        {'op':'channels.patch','channelIds':['ch_00000001'],'patch':{'seed':9876}}])
    return {'receipt':r, 'stored_seed':library.load(p)['channels'][0]['seed']}


def legacy_filter_rule_divergence(p):
    source={'type':'filter','tags':['5'],'excludeStudios':['9'],
            'duration':{'max':900},'studioSceneCount':{'max':2}}
    return {'accepted_errors':criteria.validate(source),
            'editor_projection':criteria.build_scene_filter(source),
            'playback_projection':lineup.build_scene_filter(source)}


def rejected_partial(p):
    library.save(p,fixture())
    ops=[{'op':'channels.patch','channelIds':['ch_00000001'],'patch':{'paused':True}},new_op('a'),new_op('b')]
    r=library.apply_transaction(p,expected_revision=1,request_id='partial',ops=ops)
    d=library.load(p)
    return {'receipt':r,'stored_revision':d['revision'],'stored_paused':d['channels'][0]['paused'],
            'retry':library.apply_transaction(p,expected_revision=1,request_id='partial',ops=ops)}


def crash_window(p):
    d=fixture();library.save(p,d)
    draft=copy.deepcopy(d['channels'][0]);draft['source']={'type':'tag','id':'6'}
    c=ctx(p,{'requestId':'crash','expectedRevision':1,'ops':[{'op':'channel.put','channel':draft}]})
    with patch.object(refresh,'enqueue',side_effect=RuntimeError('simulated process loss before journal write')):
        first=capture(lambda:channel_ops.op_apply_channel_changes(c))
    after=library.load(p)
    replay=channel_ops.op_apply_channel_changes(c)
    return {'first':first,'committed_source':after['channels'][0]['source'],
            'receipt':library.get_receipt(p,'crash'),'retry':replay,'pending_after_retry':refresh.read_pending(p)}


def lost_enqueued_work(p):
    d=fixture();library.save(p,d)
    signatures={c['id']:criteria.source_signature(c['source']) for c in d['channels']}
    refresh.enqueue(p,['ch_00000001'],1,signatures)
    def health(*args):
        refresh.enqueue(p,['ch_00000002'],1,signatures)
        return {'status':'ok','count':1,'sourceTotal':1}
    with patch.object(snapshots,'_channel_health',side_effect=health):
        result=refresh.process_pending(FakeClient(),p,p/'assets')
    return {'processed':result,'pending_after_concurrent_enqueue':refresh.read_pending(p)}


def lost_health(p):
    d=fixture();library.save(p,d)
    signatures={c['id']:criteria.source_signature(c['source']) for c in d['channels']}
    refresh.enqueue(p,list(signatures),1,signatures)
    with patch.object(snapshots,'_channel_health',return_value={'status':'ok','count':1,'sourceTotal':1}):
        result=refresh.process_pending(FakeClient(),p,p/'assets')
    return {'processed':result,'snapshot_channel_ids':sorted(snapshots.read_snapshot(p,p/'assets').get('channels',{}))}


def stale_schedule(p):
    d=fixture();d['channels'][0]['programming']={'mode':'explore'};library.save(p,d)
    at=1_000_000
    pub={'schema':1,'source':{'type':'tag','id':'5'},'configuration':'old-source',
         'version':'old-publication','sourceTotal':1,'preparedThrough':at+600_000,'warnings':[],'generatedAt':at-1000,
         'programs':[{'airingId':'air-old','startEpochMs':at-1000,'endEpochMs':at+599_000,
                      'item':{'id':'scene-from-removed-pool','duration':600},'block':''}]}
    snapshots.write_json(programming.path(p,'ch_00000001'),pub)
    changed=copy.deepcopy(d['channels'][0]);changed['source']={'type':'tag','id':'999'}
    library.apply_transaction(p,expected_revision=1,request_id='pool-edit',ops=[{'op':'channel.put','channel':changed}])
    return {'current_source':library.load(p)['channels'][0]['source'],
            'schedule':main._op_schedule(ctx(p,{'channelId':'ch_00000001','at':at}))}


def mode_disagreement(p):
    d=fixture();n=d['channels'][0];n.update(id='net_00000001',kind='net',number=500,groupId='grp_general',programming={'mode':'fixed','newShare':0.2})
    library.save(p,d)
    snapshots.write_json(p/'continuing-networks.json',{'enabled':True,'stage':'active','networkIds':[n['id']]})
    before=main._op_schedule(ctx(p,{'channelId':n['id']}))
    enhanced=channel_ops.op_get_channel_directory(ctx(p))
    legacy=main._op_directory(ctx(p))
    draft=copy.deepcopy(n);draft['name']='Renamed only'
    library.apply_transaction(p,expected_revision=1,request_id='rename',ops=[{'op':'channel.put','channel':draft}])
    return {'authored_mode':'fixed','enhanced_mode':next(c['programmingMode'] for c in enhanced['channels'] if c['id']==n['id']),
            'legacy_mode':legacy['networks']['channels'][0]['programmingMode'], 'schedule':before,
            'stored_policy_after_rename':next(c['programming'] for c in library.load(p)['channels'] if c['id']==n['id'])}


def stale_worker_policy(p):
    d=fixture();d['channels'][0]['programming']={'mode':'explore'};library.save(p,d)
    indexed=copy.deepcopy(d['channels'][0])
    def build(*args):
        edited=copy.deepcopy(indexed);edited['sort']='newest'
        library.apply_transaction(p,expected_revision=1,request_id='new-sort',ops=[{'op':'channel.put','channel':edited}])
        return {'schema':1,'programs':[],'auditSortUsed':indexed['sort']}
    with patch.object(programming,'index_source',return_value=[]),patch.object(programming,'build',side_effect=build):
        outcome=refresh._prepare_one(FakeClient(),p,indexed)
    return {'outcome':outcome,'latest_sort':library.load(p)['channels'][0]['sort'],
            'published_sort':json.loads(programming.path(p,indexed['id']).read_text())['auditSortUsed']}


def preview_truth(p):
    c=ctx(p,{'source':{'type':'criteria','tagsAny':['5'],'q':'must-match-this'}},FakeClient(False))
    result=channel_ops.op_preview_channel_pool(c)
    return {'preview':result,'query_filter':c.client.calls[-1]['filter']}


def migration_drift(p):
    d=fixture()
    snapshots.write_json(p/'catalog.json',{'schemaVersion':1,'revision':0,'settings':{},'channels':[]})
    net=json.loads((ROOT/'justwatch/networks.json').read_text())
    net['channels'][0]['name']='UNEXPLAINED AUDIT DRIFT'
    snapshots.write_json(p/'networks.json',net)
    cmd=[sys.executable,str(ROOT/'tools/migrate_channel_library.py'),'--data-dir',str(p),'--staging-dir',str(p/'staging'),'--backup-root',str(p/'backups')]
    dry=subprocess.run(cmd+['--dry-run'],capture_output=True,text=True)
    apply=subprocess.run(cmd+['--apply'],capture_output=True,text=True)
    return {'dry_exit':dry.returncode,'apply_exit':apply.returncode,
            'reported_drift': 'DRIFT:' in apply.stderr,'library_created_despite_drift':library.exists(p)}


def rollback_overlay(p):
    snapshots.write_json(p/'catalog.json',{'schemaVersion':1,'revision':0,'settings':{},'channels':[]})
    snapshots.write_json(p/'programming'/'original.json',{'old':True})
    backup=migration.make_backup(p,p/'backups')
    snapshots.write_json(p/'programming'/'newer.json',{'new':True})
    snapshots.write_json(p/'continuing-networks.json',{'enabled':True,'networkIds':['net_00000001']})
    snapshots.write_json(p/'channel-library-pending.json',[{'channelId':'net_00000001'}])
    result=subprocess.run([sys.executable,str(ROOT/'tools/migrate_channel_library.py'),'--data-dir',str(p),'--restore',str(backup)],capture_output=True,text=True)
    return {'exit':result.returncode,'newer_publication_survives':(p/'programming'/'newer.json').exists(),
            'previously_absent_rollout_survives':(p/'continuing-networks.json').exists(),
            'pending_journal_survives':(p/'channel-library-pending.json').exists()}


def validation(p):
    library.save(p,fixture())
    draft=copy.deepcopy(fixture()['channels'][0]);draft['source']={'type':'studio','id':'999999999999'}
    client=FakeClient()
    validation=channel_ops.op_validate_channel_changes(ctx(p,{'ops':[{'op':'channel.put','channel':draft}]},client))
    return {'missing_entity_validation':validation,'validation_stash_queries':len(client.calls),
            'mixed_id_list_errors':criteria.validate({'type':'criteria','tagsAny':['5','bad-id']}),
            'invalid_date_errors':criteria.validate({'type':'criteria','date':{'from':'2026-99-99'}}),
            'dynamic_max_only':criteria.build_scene_filter({'type':'criteria','studioSceneCount':{'max':2}}),
            'dynamic_min_and_max':criteria.build_scene_filter({'type':'criteria','studioSceneCount':{'min':1,'max':2}}),
            'dynamic_epoch':lineup.effective_epoch({'type':'criteria','studioSceneCount':{'max':2}})}


CASES=[rules_playback,legacy_filter_rule_divergence,patch_identity,rejected_partial,crash_window,lost_enqueued_work,lost_health,
       stale_schedule,mode_disagreement,stale_worker_policy,preview_truth,migration_drift,rollback_overlay,validation]
if __name__=='__main__':
    output={'plugin_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'cases':{}}
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix='jw-curation-audit-') as name:
            output['cases'][case.__name__]=capture(lambda:case(Path(name)))
    print(json.dumps(output,indent=2))
