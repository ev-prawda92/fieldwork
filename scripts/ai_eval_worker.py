"""Run a single pass of queued workflow evaluations against your agent runner.

FIELDWORK_URL=https://fieldwork.example FIELDWORK_TOKEN=fwp_... \
FIELDWORK_AGENT_RUNNER_URL=https://runner.example/evaluate \
python scripts/ai_eval_worker.py

Runner contract: POST receives {version_id, fingerprint, artifact_ref, spec}.
It executes every spec.evals input against that exact agent artifact and returns
{version_id, fingerprint, artifact_ref, evidence_ref, runner, traces}. Traces
match the Trace schema in /openapi.json. The worker rejects mismatched versions
and artifacts, then submits observed evidence. Runner must return genuinely
observed traces; this protocol doesn't cryptographically attest execution.
No secrets or tokens are included in the runner payload. Use a separately
scoped runner token. Schedule this script in your own job system for continuous
processing. Changes supersede pending jobs; stale submissions return 409.
"""
import os
import sys
from urllib.parse import quote, urlparse

import httpx


def valid_url(value):
    parsed = urlparse(value)
    local = parsed.hostname in ('127.0.0.1', 'localhost')
    if not parsed.hostname or parsed.username or parsed.password or (parsed.scheme!='https' and not (local and parsed.scheme=='http')):
        raise ValueError('Use HTTPS endpoints, or HTTP on localhost for development')
    return value.rstrip('/')


def process(client, fieldwork_url, runner_url, token, runner_token=''):
    auth={'Authorization':'Bearer '+token}
    queue=client.get(fieldwork_url+'/api/ai/eval-jobs',headers=auth)
    queue.raise_for_status()
    completed, failed=0,0
    for job in queue.json()['jobs']:
        path=fieldwork_url+'/api/deployments/'+quote(job['deployment_id'],safe='')+'/ai'
        try:
            workflow=client.get(path,headers=auth);workflow.raise_for_status()
            version=workflow.json()['current']
            if not version or version['id']!=job['version_id']:
                continue
            payload={'version_id':version['id'],'fingerprint':version['fingerprint'],
                     'artifact_ref':version['spec']['agent']['artifact_ref'],'spec':version['spec']}
            response=client.post(runner_url,json=payload,headers={'Authorization':'Bearer '+runner_token} if runner_token else {})
            response.raise_for_status()
            observed=response.json()
            if any(observed.get(k)!=payload[k] for k in ('version_id','fingerprint','artifact_ref')):
                raise ValueError('Runner evidence does not match queued version and artifact')
            if not observed.get('evidence_ref') or not observed.get('runner') or 'traces' not in observed:
                raise ValueError('Runner did not supply evidence reference, identity and observed traces')
            result=client.post(path+'/evals',headers=auth,json={
                'version_id':version['id'],'mode':'observed','artifact_ref':payload['artifact_ref'],
                'runner':observed['runner'],'evidence_ref':observed['evidence_ref'],'traces':observed['traces']})
            result.raise_for_status()
            print(f"{job['id']}: {'passed' if result.json()['passed'] else 'failed assertions'}")
            completed+=1
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # Do not print request headers, bodies, provider errors or credentials.
            print(f"{job['id']}: {type(exc).__name__}; job remains pending if no evidence was stored",file=sys.stderr)
            failed+=1
    return {'completed':completed,'failed':failed}


def main():
    try:
        base=valid_url(os.environ['FIELDWORK_URL']);runner=valid_url(os.environ['FIELDWORK_AGENT_RUNNER_URL'])
        token=os.environ['FIELDWORK_TOKEN']
        with httpx.Client(timeout=60,follow_redirects=False,trust_env=False) as client:
            result=process(client,base,runner,token,os.environ.get('FIELDWORK_AGENT_RUNNER_TOKEN',''))
        print(f"Evaluations recorded: {result['completed']}; worker errors: {result['failed']}")
        return int(result['failed']>0)
    except (KeyError, ValueError, httpx.HTTPError) as exc:
        print(f"Worker setup failed: {type(exc).__name__}. Check endpoint URLs, tokens and connectivity.",file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
