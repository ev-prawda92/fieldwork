"""Onboarding contract, permission, atomicity, replay and model-boundary tests."""
import copy
import json

import pytest
from fastapi import HTTPException
from fieldwork import onboarding
from .conftest import H

CSV = 'Client,Project,Phase,Risk\nExample Co,Agent rollout,Discover,At risk\n'


def preview(client, **kwargs):
    response = client.post('/api/onboarding/preview', headers=H('head'), json={'csv_text':CSV, **kwargs})
    assert response.status_code==200, response.text
    return response.json()


def apply(client, plan, who='head', reviewed=True):
    return client.post(f"/api/onboarding/{plan['id']}/activate", headers=H(who), json={'reviewed':reviewed})


def test_preview_is_non_destructive_and_activation_is_replay_safe(client):
    count = client.conn.execute('SELECT COUNT(*) n FROM deployments').fetchone()['n']
    plan = preview(client)
    assert plan['mode']=='guided' and plan['deployments'][0]['health']=='at_risk'
    assert client.conn.execute('SELECT COUNT(*) n FROM deployments').fetchone()['n']==count
    assert apply(client,plan,reviewed=False).status_code==422
    result = apply(client,plan)
    assert result.status_code==200, result.text
    assert apply(client,plan).json()==result.json()
    assert client.conn.execute('SELECT COUNT(*) n FROM deployments').fetchone()['n']==count+1
    assert client.get('/api/audit/verify',headers=H('head')).json()['ok']


def test_existing_permissions_and_engines_preserved(client,monkeypatch):
    cfg = client.get('/api/config',headers=H('head')).json()['config']
    names = [s['name'] for s in cfg['stages']]; names[0]='Kickoff'
    suggestion=onboarding.Suggestion(customer='Client',deployment='Project',stage='Phase',health='Risk',stage_names=names,questions=[])
    monkeypatch.setattr(onboarding,'suggest',lambda *args:suggestion)
    plan=preview(client,use_ai=True)
    assert plan['stages'][0]['name']=='Kickoff'
    assert apply(client,plan).status_code==200
    after=client.get('/api/config',headers=H('head')).json()['config']
    for key in ['permissions','roles','engines','sso']:
        assert after[key]==cfg[key]
    assert [s['key'] for s in after['stages']]==[s['key'] for s in cfg['stages']]


def test_permissions_and_actor_tenant_binding(client):
    for who in ('em','customer','fde'):
        assert client.post('/api/onboarding/preview',headers=H(who),json={}).status_code==403
    plan=preview(client)
    assert apply(client,plan,who='other_tenant').status_code==404
    assert apply(client,plan,who='em').status_code==403
    assert client.post('/api/onboarding/preview',json={}).status_code==401


def test_stale_expired_and_duplicate_plans_fail_before_writes(client):
    plan=preview(client)
    cfg=client.get('/api/config',headers=H('head')).json()['config']
    cfg['branding']['product_name']='Updated workspace'
    assert client.put('/api/config',headers=H('head'),json=cfg).status_code==200
    assert apply(client,plan).status_code==409
    plan=preview(client)
    client.conn.execute('UPDATE onboarding_plans SET expires_at=? WHERE id=?',('2000-01-01',plan['id']));client.conn.commit()
    assert apply(client,plan).status_code==409
    plan=preview(client)
    assert apply(client,plan).status_code==200
    new_plan=preview(client)
    before=client.conn.execute('SELECT COUNT(*) n FROM deployments').fetchone()['n']
    assert apply(client,new_plan).status_code==409
    assert client.conn.execute('SELECT COUNT(*) n FROM deployments').fetchone()['n']==before


@pytest.mark.parametrize('bad',[
    'Customer,Deployment\nExample,A\nExample,A',
    'Customer,Deployment,Stage\nExample,A,imaginary',
    'Customer,Deployment,Health\nExample,A,invented',
    'Customer,Deployment\n,A',
    'Client,Account,Project\nExample,Example,A',
])
def test_invalid_or_ambiguous_rows_never_get_an_activatable_plan(client,bad):
    plan=preview(client,csv_text=bad)
    assert plan['errors'] and 'id' not in plan


def test_malformed_csv_and_limits(client):
    for text in ['Customer,Customer\nA,B','Customer,Deployment\nA,B,C','Customer,Deployment\nA',
                 'Customer,Deployment\n'+'\n'.join(f'A,Project {i}' for i in range(201))]:
        assert client.post('/api/onboarding/preview',headers=H('head'),json={'csv_text':text}).status_code==422


def test_unknown_model_headers_and_ai_failure_never_write(client,monkeypatch):
    cfg=client.get('/api/config',headers=H('head')).json()['config']
    suggestion=onboarding.Suggestion(customer='Invented',deployment='Project',stage='Phase',health='Risk',stage_names=[s['name'] for s in cfg['stages']],questions=[])
    monkeypatch.setattr(onboarding,'suggest',lambda *args:suggestion)
    assert client.post('/api/onboarding/preview',headers=H('head'),json={'csv_text':CSV,'use_ai':True}).status_code==422
    def fail(*args): raise HTTPException(502,'Provider unavailable')
    monkeypatch.setattr(onboarding,'suggest',fail)
    assert client.post('/api/onboarding/preview',headers=H('head'),json={'csv_text':CSV,'use_ai':True}).status_code==502
    assert client.conn.execute('SELECT COUNT(*) n FROM onboarding_plans').fetchone()['n']==0


def test_ai_opt_in_and_empty_workspace_setup(client,monkeypatch):
    def forbidden(*args):raise AssertionError('No model call without opt-in')
    monkeypatch.setattr(onboarding,'suggest',forbidden)
    plan=preview(client,csv_text='')
    assert apply(client,plan).json()['created']==0


def test_atomic_rollback_after_mid_import_error(client,monkeypatch):
    plan=preview(client,csv_text='Customer,Deployment\nExample,A\nExample,B')
    before=client.conn.execute('SELECT COUNT(*) n FROM deployments').fetchone()['n']
    original=client.conn.execute
    writes=0
    def execute(sql, params=()):
        nonlocal writes
        if sql.startswith('INSERT INTO deployments'):
            writes+=1
            if writes==2: raise RuntimeError('Simulated storage failure')
        return original(sql,params)
    monkeypatch.setattr(client.conn,'execute',execute)
    with pytest.raises(RuntimeError):apply(client,plan)
    assert original('SELECT COUNT(*) n FROM deployments').fetchone()['n']==before
    monkeypatch.setattr(client.conn,'execute',original)
    assert apply(client,plan).json()['created']==2


def test_provider_sends_only_description_and_headings(monkeypatch):
    captured={}
    monkeypatch.setenv('FIELDWORK_ONBOARDING_OPENAI_API_KEY','test-only-key')
    monkeypatch.setenv('FIELDWORK_ONBOARDING_MODEL','test-model')
    cfg={'stages':[{'name':'Discover'},{'name':'Go live'}]}
    result={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({
        'customer':'Client','deployment':'Project','stage':'','health':'','stage_names':['Discover','Go live'],'questions':[]})}]}]}
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
        def iter_bytes(self):yield json.dumps(result).encode()
    class Client:
        def __init__(self,**kwargs):captured['options']=kwargs
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def stream(self,method,url,**kwargs):
            captured.update(method=method,url=url,**kwargs);return Response()
    monkeypatch.setattr(onboarding.httpx,'Client',Client)
    assert onboarding.suggest('We deploy software',['Client','Project'],cfg['stages']).customer=='Client'
    assert json.loads(captured['json']['input'])=={'description':'We deploy software','column_headings':['Client','Project'],'existing_stages':['Discover','Go live']}
    assert captured['json']['store'] is False
    assert captured['options']['follow_redirects'] is False
    assert captured['url']=='https://api.openai.com/v1/responses'


def test_demo_reset_clears_previews(client):
    from fieldwork.seed import reseed_demo
    preview(client)
    reseed_demo(client.conn)
    assert client.conn.execute('SELECT COUNT(*) n FROM onboarding_plans').fetchone()['n']==0
