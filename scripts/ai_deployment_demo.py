"""Reproducible, isolated Fieldwork + Cortex demo; no external model or system calls.
Run: python scripts/ai_deployment_demo.py
Uses a temporary workspace and synthetic rehearsals. It deliberately cannot
claim production readiness. See tests/test_ai_deployments.py for the full
rollout contract using explicitly test-only observed fixtures.
"""
import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from fieldwork.app import create_app
from fieldwork.seed import seed, DEMO_TOKENS


def main():
    with tempfile.TemporaryDirectory(prefix='fieldwork-demo-') as tmp:
        os.environ['FIELDWORK_KEY_FILE']=str(Path(tmp)/'key')
        path=str(Path(tmp)/'demo.db');seed(path)
        with TestClient(create_app(path)) as client:
            fde={'Authorization':'Bearer '+DEMO_TOKENS['fde']}
            head={'Authorization':'Bearer '+DEMO_TOKENS['head']}
            prefix='/api/deployments/dep_northfield/ai'
            def call(method,path,headers=fde,body=None):
                response=client.request(method,path,headers=headers,json=body)
                response.raise_for_status()
                return response.json()
            spec=call('GET',prefix+'/example')
            version=call('PUT',prefix+'/spec',body={'spec':spec})
            print('1. Versioned workflow: Box → support agent → Salesforce draft → supervisor')
            print('   Missing Box access, Cortex activation and executable agent artifact are visible.')
            spec['systems'][0]['access']='verified'
            spec['controls']['authority_enabled']=True
            spec['agent']['artifact_ref']='demo-only:agent-fixture'
            version=call('PUT',prefix+'/spec',body={'spec':spec,'base_version_id':version['id']})
            request={'version_id':version['id'],'tool_id':'salesforce.draft','environment':'staging','target_system':'salesforce',
                     'data_scope':'support','financial_impact':0.0,'actions_last_hour':0,'evidence':[{'type':'source','current':True,'reference':'demo-only:support-case'}]}
            result=call('POST',prefix+'/cortex/check',body=request)
            assert result['decision']=='HUMAN_REVIEW'
            print('2. Cortex requires human review before a Salesforce write.')
            approval=call('POST',prefix+'/cortex/approvals',body=request)
            call('POST','/api/approvals/'+approval['id']+'/decide',headers=head,body={'approve':True,'note':'Synthetic demo review'})
            result=call('POST',prefix+'/cortex/check',body={**request,'approval_id':approval['id']})
            assert result['decision']=='ALLOW' and result['executed'] is False
            print('3. A second person approves the exact scoped action. No external write executes.')
            failed=call('POST',prefix+'/evals',body={'version_id':version['id'],'simulate_failure':'answer'})
            assert not failed['passed']
            print('4. Synthetic regression fails. Deployment assistant proposes remediation.')
            passed=call('POST',prefix+'/evals',body={'version_id':version['id']})
            assert passed['passed']
            workflow=call('GET',prefix)
            assert not workflow['readiness']['ready']
            print('5. Rehearsal passes, but production readiness still requires real observed traces.')
            old=version
            spec['agent']['model']='demo-upgrade'
            version=call('PUT',prefix+'/spec',body={'spec':spec,'base_version_id':version['id']})
            queued=call('GET','/api/ai/eval-jobs')['jobs']
            assert len(queued)==1 and queued[0]['version_id']==version['id']
            print(f"6. Model upgrade creates v{version['revision']}, supersedes the old job and queues {len(spec['evals'])} evals.")
            assert call('GET','/api/audit/verify',headers=head)['ok']
            print('7. Audit chain verifies. Demo ran locally with synthetic data only.')
            print(json.dumps({'workflow':spec['workflow'],'version':version['revision'],'agent':spec['agent']['name'],
                              'cortex':'integrated','evals_queued':len(spec['evals']),'real_agent_executed':False},indent=2))


if __name__=='__main__':main()
