"""Version/evidence integrity, tenancy, release authority and Cortex integration."""
import copy
import json

import pytest
from fieldwork.ai_deployments import example_spec, rehearsal
from .conftest import H

P = '/api/deployments/dep_northfield/ai'


def spec_ready():
    spec = example_spec()
    spec['systems'][0]['access']='verified'
    spec['controls']['authority_enabled']=True
    spec['agent']['artifact_ref']='git:agent@fixture-revision'
    return spec


def save(client, spec=None, base=None, who='fde'):
    r=client.put(P+'/spec',headers=H(who),json={'spec':spec or spec_ready(),'base_version_id':base})
    assert r.status_code==200,r.text
    return r.json()


def run(client,v,mode='observed',traces=None,who='fde'):
    payload={'version_id':v['id'],'mode':mode}
    if mode=='observed':
        payload.update(runner='test-fixture-runner',evidence_ref='test-only:runner-evidence',artifact_ref=v['spec']['agent']['artifact_ref'],
                       traces=traces if traces is not None else [t.model_dump() for t in rehearsal(v['spec'])])
    return client.post(P+'/evals',headers=H(who),json=payload)


def release(client,v,who='fde'):
    return client.post(P+'/releases',headers=H(who),json={'version_id':v['id'],'reviewed':True})


def request(v,tool='salesforce.draft',**kwargs):
    return {'version_id':v['id'],'tool_id':tool,'environment':'staging','data_scope':'support','target_system':'salesforce' if tool=='salesforce.draft' else 'box',
            'financial_impact':0.0,'actions_last_hour':0,'evidence':[{'type':'source','current':True,'reference':'test-only:case-1'}],**kwargs}


def test_spec_versions_stale_edits_and_graph(client):
    v=save(client)
    assert v['revision']==1
    r=client.get(P,headers=H('fde')).json()
    assert len(r['graph']['edges'])>5 and not r['readiness']['ready']
    assert save(client,base=v['id'])['id']==v['id']
    assert client.put(P+'/spec',headers=H('fde'),json={'spec':spec_ready()}).status_code==409
    spec=spec_ready();spec['agent']['model']='new-model'
    v2=save(client,spec,base=v['id'])
    assert v2['revision']==2 and v2['fingerprint']!=v['fingerprint']
    assert v2['changes']['fields']==['agent']
    assert len(v2['changes']['affected_evals'])==5
    assert run(client,v).status_code==409
    assert client.get('/api/audit/verify',headers=H('head')).json()['ok']


def test_customer_and_other_workspace_never_see_internal_ai(client):
    save(client)
    for who in ('customer','other_tenant'):
        r=client.get(P,headers=H(who))
        assert r.status_code in (403,404)
        assert client.get('/api/ai/registry',headers=H(who)).json()['deployments']==[]
        assert client.get('/api/ai/memory',headers=H(who)).json()['lessons']==[]
    assert client.get(P).status_code==401
    assert client.get('/api/deployments/dep_harborview/ai',headers=H('fde')).status_code==404


def test_rehearsal_cannot_authorize_rollout(client):
    v=save(client)
    assert run(client,v,mode='rehearsal').json()['passed']
    assert not client.get(P,headers=H('fde')).json()['readiness']['ready']
    assert release(client,v).status_code==409
    body={'version_id':v['id'],'mode':'rehearsal','simulate_failure':'draft'}
    r=client.post(P+'/evals',headers=H('fde'),json=body)
    assert not r.json()['passed']


def test_complete_observed_evidence_and_separate_review(client):
    v=save(client)
    r=run(client,v);assert r.status_code==200,r.text
    assert client.get(P,headers=H('fde')).json()['readiness']['ready']
    rollout=release(client,v).json()
    assert release(client,v).json()['id']==rollout['id']
    endpoint=P+f"/releases/{rollout['id']}/decide"
    assert client.post(endpoint,headers=H('fde'),json={'approve':True,'note':'Reviewed'}).status_code==403
    approval=client.post(endpoint,headers=H('head'),json={'approve':True,'note':'Fixture evidence reviewed'})
    assert approval.json()['status']=='approved_for_rollout' and not approval.json()['executed']
    assert client.post(endpoint,headers=H('head'),json={'approve':True,'note':'Repeat'}).status_code==409
    assert client.get('/api/audit/verify',headers=H('head')).json()['ok']


def test_failed_new_run_revokes_readiness_and_invalidates_review(client):
    v=save(client);assert run(client,v).json()['passed']
    rollout=release(client,v).json()
    traces=[t.model_dump() for t in rehearsal(v['spec'])];traces[0]['tools_called']=['unknown-tool']
    assert not run(client,v,traces=traces).json()['passed']
    assert not client.get(P,headers=H('fde')).json()['readiness']['ready']
    assert client.post(P+f"/releases/{rollout['id']}/decide",headers=H('head'),json={'approve':True,'note':'Review'}).status_code==409


def test_change_or_expiry_invalidates_release(client):
    v=save(client);run(client,v)
    rollout=release(client,v).json()
    client.conn.execute("UPDATE ai_eval_runs SET created_at='2000-01-01T00:00:00Z' WHERE version_id=?",(v['id'],));client.conn.commit()
    assert not client.get(P,headers=H('fde')).json()['readiness']['ready']
    assert release(client,v).status_code==409
    spec=spec_ready();spec['agent']['prompt']+=' New prompt.'
    save(client,spec,base=v['id'])
    assert client.post(P+f"/releases/{rollout['id']}/decide",headers=H('head'),json={'approve':True,'note':'Review'}).status_code==409


def test_author_and_evidence_submitter_cannot_review_even_as_head(client):
    v=save(client,who='head');run(client,v,who='fde')
    rollout=release(client,v).json()
    assert client.post(P+f"/releases/{rollout['id']}/decide",headers=H('head'),json={'approve':True,'note':'Review'}).status_code==403


def test_trace_failures_unknown_ids_and_missing_cases(client):
    v=save(client)
    traces=[t.model_dump() for t in rehearsal(v['spec'])]
    assert not run(client,v,traces=traces[:-1]).json()['passed']
    traces[2]['human_approved']=False
    assert not run(client,v,traces=traces).json()['passed']
    assert run(client,v,traces=traces+traces[:1]).status_code==422
    traces[0]['case_id']='unknown'
    assert run(client,v,traces=traces).status_code==422
    assert client.post(P+'/evals',headers=H('fde'),json={'version_id':v['id'],'mode':'observed'}).status_code==422


@pytest.mark.parametrize('change', ['unknown_system','duplicate_case','bad_assertion','bad_dependency','extra','nan'])
def test_invalid_spec_has_no_writes(client,change):
    spec=spec_ready()
    if change=='unknown_system':spec['tools'][0]['system_id']='unknown'
    if change=='duplicate_case':spec['evals'].append(copy.deepcopy(spec['evals'][0]))
    if change=='bad_assertion':spec['evals'][0]['assertions'][0]['value']='wrong'
    if change=='bad_dependency':spec['evals'][0]['depends_on']=['invented']
    if change=='extra':spec['agent']['secret']='not-allowed'
    if change=='nan':spec['kpi']['target']='NaN'
    r=client.put(P+'/spec',headers=H('fde'),json={'spec':spec})
    assert r.status_code==422,r.text
    assert client.conn.execute('SELECT COUNT(*) n FROM ai_versions').fetchone()['n']==0


def test_assistant_review_replay_and_workspace_stage_keys(client):
    v=save(client,example_spec())
    plan=client.get(P+'/plan',headers=H('fde')).json()
    assert plan['tasks'] and plan['external_actions']==[]
    assert client.post(P+'/plan/apply',headers=H('fde'),json={'version_id':v['id'],'reviewed':False}).status_code==422
    body={'version_id':v['id'],'reviewed':True}
    r=client.post(P+'/plan/apply',headers=H('fde'),json=body)
    assert r.status_code==200,r.text
    assert len(r.json()['created'])==len(plan['tasks'])
    assert client.post(P+'/plan/apply',headers=H('fde'),json=body).json()['created']==[]


def test_cortex_gates_real_approval_and_binds_context(client):
    v=save(client)
    body=request(v)
    r=client.post(P+'/cortex/check',headers=H('fde'),json=body)
    assert r.json()['decision']=='HUMAN_REVIEW' and not r.json()['executed']
    approval=client.post(P+'/cortex/approvals',headers=H('fde'),json=body)
    assert approval.status_code==200,approval.text
    aid=approval.json()['id']
    assert client.post('/api/approvals/'+aid+'/decide',headers=H('head'),json={'approve':True}).status_code==200
    body['approval_id']=aid
    assert client.post(P+'/cortex/check',headers=H('fde'),json=body).json()['decision']=='ALLOW'
    assert client.post(P+'/cortex/check',headers=H('fde'),json={**body,'financial_impact':1.0}).status_code==409
    v2=save(client,{**spec_ready(),'description':'Changed'},base=v['id'])
    assert client.post(P+'/cortex/check',headers=H('fde'),json={**body,'version_id':v2['id']}).status_code==409
    assert client.get('/api/audit/verify',headers=H('head')).json()['ok']


def test_cortex_blocks_missing_evidence_scopes_limits_and_unlisted_tools(client):
    v=save(client)
    for overrides,expected in [({'evidence':[]},'REQUEST_MORE_EVIDENCE'),({'environment':'production'},'BLOCK'),({'data_scope':'payroll'},'BLOCK'),
                               ({'financial_impact':1.0},'BLOCK'),({'actions_last_hour':60},'BLOCK'),({'tool_id':'not-delegated'},'BLOCK')]:
        r=client.post(P+'/cortex/check',headers=H('fde'),json=request(v,**overrides))
        assert r.status_code==200,r.text
        assert r.json()['decision']==expected
    assert client.get(P+'/cortex',headers=H('fde')).json()['passed']
    assert client.post(P+'/cortex/check',headers=H('fde'),json=request(v,tool='box.search')).json()['decision']=='ALLOW'


def test_generic_approval_cannot_forge_a_cortex_binding(client):
    v=save(client)
    from fieldwork.ai_deployments import fingerprint
    body=request(v)
    context={k: val for k,val in body.items() if k not in ('version_id','tool_id')};context['action']=body['tool_id']
    detail=json.dumps({'version_id':v['id'],'context_hash':fingerprint(context)})
    aid=client.post('/api/deployments/dep_northfield/approvals',headers=H('fde'),json={'agent':'Fake','request':'Anything','detail':detail}).json()['id']
    client.post('/api/approvals/'+aid+'/decide',headers=H('head'),json={'approve':True})
    assert client.post(P+'/cortex/check',headers=H('fde'),json={**body,'approval_id':aid}).status_code==409


def test_tenant_memory_requires_confirmation_and_demo_reset(client):
    v=save(client)
    lesson=client.post(P+'/lessons',headers=H('fde'),json={'version_id':v['id'],'title':'Scope missing','pattern':'Box support','evidence':'Customer security review required content.read'}).json()
    assert client.get('/api/ai/memory',headers=H('head')).json()['lessons']==[]
    endpoint=P+'/lessons/'+lesson['id']+'/confirm'
    assert client.post(endpoint,headers=H('fde')).status_code==403
    assert client.post(endpoint,headers=H('head')).status_code==200
    assert len(client.get('/api/ai/memory',headers=H('head')).json()['lessons'])==1
    from fieldwork.seed import reseed_demo
    reseed_demo(client.conn)
    assert client.get(P,headers=H('fde')).json()['current'] is None


def test_discovery_proposes_without_writing_or_granting_access(client):
    r=client.post(P+'/discover',headers=H('fde'),json={'workflow':'Customer onboarding','trigger':'Won deal','systems':[{'id':'crm','name':'CRM'}],'handoffs':['sales','support']})
    assert r.status_code==200,r.text
    assert r.json()['spec']['systems'][0]['access']=='unknown'
    assert r.json()['spec']['tools']==[]
    assert client.conn.execute('SELECT COUNT(*) n FROM ai_versions').fetchone()['n']==0


def test_change_queues_evaluations_and_rehearsals_do_not_complete_jobs(client):
    v=save(client)
    jobs=client.get('/api/ai/eval-jobs',headers=H('fde')).json()['jobs']
    assert len(jobs)==1 and jobs[0]['version_id']==v['id']
    run(client,v,mode='rehearsal')
    assert len(client.get('/api/ai/eval-jobs',headers=H('fde')).json()['jobs'])==1
    run(client,v)
    assert client.get('/api/ai/eval-jobs',headers=H('fde')).json()['jobs']==[]
    spec=spec_ready();spec['agent']['model']='upgrade-1'
    v2=save(client,spec,base=v['id'])
    spec['agent']['model']='upgrade-2'
    v3=save(client,spec,base=v2['id'])
    jobs=client.get('/api/ai/eval-jobs',headers=H('fde')).json()['jobs']
    assert len(jobs)==1 and jobs[0]['version_id']==v3['id']
    assert client.get('/api/ai/eval-jobs',headers=H('customer')).json()['jobs']==[]
    assert client.get('/api/ai/eval-jobs',headers=H('other_tenant')).json()['jobs']==[]


def test_production_cortex_requires_review_and_revokes_on_change(client):
    spec=spec_ready()
    for tool in spec['tools']:tool['environments']=['staging','production']
    v=save(client,spec)
    body=request(v,tool='box.search',environment='production')
    assert client.post(P+'/cortex/check',headers=H('fde'),json=body).json()['decision']=='BLOCK'
    run(client,v);rollout=release(client,v).json()
    approved=client.post(P+f"/releases/{rollout['id']}/decide",headers=H('head'),json={'approve':True,'note':'Approve fixture rollout'})
    assert approved.status_code==200,approved.text
    assert client.post(P+'/cortex/check',headers=H('fde'),json=body).json()['decision']=='ALLOW'
    assert client.get(P,headers=H('head')).json()['releases'][0]['effective']
    traces=[t.model_dump() for t in rehearsal(v['spec'])];traces[0]['tools_called']=['unknown']
    run(client,v,traces=traces)
    assert client.post(P+'/cortex/check',headers=H('fde'),json=body).json()['decision']=='BLOCK'
    assert not client.get(P,headers=H('head')).json()['releases'][0]['effective']


def test_atomic_spec_queue_rollback(client,monkeypatch):
    original=client.conn.execute
    def execute(sql,params=()):
        if sql.startswith('INSERT INTO ai_eval_jobs'):raise RuntimeError('storage failure')
        return original(sql,params)
    monkeypatch.setattr(client.conn,'execute',execute)
    with pytest.raises(RuntimeError):save(client)
    assert original('SELECT COUNT(*) n FROM ai_versions').fetchone()['n']==0


def test_mcp_ai_tools_use_api_permissions(client):
    msg={'jsonrpc':'2.0','id':1,'method':'tools/list'}
    names={t['name'] for t in client.post('/mcp',headers=H('fde'),json=msg).json()['result']['tools']}
    assert {'get_ai_workflow','check_cortex_authority','ai_eval_queue','plan_ai_deployment'}<=names
    msg.update(method='tools/call',params={'name':'get_ai_workflow','arguments':{'deployment_id':'dep_northfield'}})
    assert not client.post('/mcp',headers=H('fde'),json=msg).json()['result']['isError']
    assert client.post('/mcp',headers=H('customer'),json=msg).json()['result']['isError']


def test_worker_validates_artifact_and_keeps_tokens_separate():
    import httpx
    from scripts.ai_eval_worker import process
    seen=[]
    version={'id':'v1','fingerprint':'sha256:spec','spec':{'agent':{'artifact_ref':'git:agent@1'},'evals':[]}}
    def handler(request):
        seen.append(request)
        if request.url.path=='/api/ai/eval-jobs':return httpx.Response(200,json={'jobs':[{'id':'j1','deployment_id':'d1','version_id':'v1'}]})
        if request.url.path=='/api/deployments/d1/ai':return httpx.Response(200,json={'current':version})
        if request.url.host=='runner.example':
            return httpx.Response(200,json={'version_id':'v1','fingerprint':'sha256:spec','artifact_ref':'git:agent@1','evidence_ref':'log:1','runner':'external','traces':[]})
        return httpx.Response(200,json={'passed':True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert process(c,'https://fieldwork.example','https://runner.example/evaluate','fieldwork-token','runner-token')=={'completed':1,'failed':0}
    runner_request=next(r for r in seen if r.url.host=='runner.example')
    assert runner_request.headers['authorization']=='Bearer runner-token'
    assert 'fieldwork-token' not in runner_request.content.decode()
    def mismatched(request):
        response=handler(request)
        if request.url.host=='runner.example':return httpx.Response(200,json={**response.json(),'artifact_ref':'wrong'})
        return response
    with httpx.Client(transport=httpx.MockTransport(mismatched)) as c:
        assert process(c,'https://fieldwork.example','https://runner.example/evaluate','fieldwork-token')['failed']==1


def test_seed_after_migration_and_repeated_seed_reset_all_tables(db_url):
    """Container startup migrates before seeding; Postgres fixtures reuse a database."""
    import re
    from fieldwork import db
    from fieldwork.app import create_app
    from fieldwork.seed import seed
    from fastapi.testclient import TestClient
    conn=db.connect(db_url)
    try:
        db.init(conn)
        seed(conn)
        c=TestClient(create_app(db_url))
        try:
            v=save(c)
            run(c,v)
            rollout=release(c,v)
            assert rollout.status_code==200,rollout.text
            lesson=c.post(P+'/lessons',headers=H('fde'),json={'version_id':v['id'],'title':'Reset example','pattern':'Support','evidence':'Fixture evidence'})
            assert lesson.status_code==200,lesson.text
            body=request(v)
            assert c.post(P+'/cortex/check',headers=H('fde'),json=body).status_code==200
            assert c.post(P+'/cortex/approvals',headers=H('fde'),json=body).status_code==200
            preview=c.post('/api/onboarding/preview',headers=H('head'),json={'csv_text':'Client,Project\nExample,Deployment'})
            assert preview.status_code==200,preview.text
        finally:
            if c.app.state.conn.dialect=='postgres':c.app.state.conn.raw.close()
        seed(conn)
        for name in ('ai_versions','ai_eval_jobs','ai_eval_runs','ai_releases','ai_lessons','ai_cortex_approvals','ai_authority_decisions','onboarding_plans'):
            assert conn.execute(f'SELECT COUNT(*) n FROM {name}').fetchone()['n']==0
        db.reset(conn)
        # Every table created by a migration must be removed before rerunning them.
        names=set(re.findall(r'CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)', '\n'.join(sql for _,_,sql in db.MIGRATIONS)))
        if conn.dialect=='postgres':
            remaining={r['table_name'] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}
        else:
            remaining={r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert not names & remaining
        db.init(conn)
        seed(conn)
    finally:
        conn.raw.close()
