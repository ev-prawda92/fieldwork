# Launch checklist

## 1. Decide the license (before anything is public)
`LICENSE.md` explains the two options. Recommended: FSL-1.1-ALv2, which is free to use and self-host, but nobody else can sell it as a hosted service.

## 2. Put the code in a private GitHub repo
Create an empty **private** repo, e.g. `fieldwork`, then push this code there (Claude can push once the repo exists and is connected).

## 3. Deploy on Render (about 15 minutes, roughly $15–25/month)
1. In Render: **New → Blueprint**, pick the repo. `render.yaml` creates the web service, a Postgres database and a weekday digest job.
2. Generate an encryption key on your computer and paste it as `FIELDWORK_SECRET_KEYS` (on both the web service and the digest job):
   `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   Keep a copy somewhere safe. Losing it means re-entering integration secrets.
3. Set `FIELDWORK_ACCESS_PASSWORD` to keep the site private, and `FIELDWORK_PUBLIC_URL` to the site's https URL.
4. Deploy. The demo workspace seeds itself on first start and resets every 60 minutes.
5. Usage numbers: `curl -H "X-Operator-Token: <FIELDWORK_OPERATOR_TOKEN from Render>" https://<site>/api/operator/metrics`

For a real (non-demo) workspace, set `FIELDWORK_DEMO=0` and `FIELDWORK_DEMO_RESET_MINUTES=0`.

## 4. Before going public
- [ ] License chosen and `LICENSE.md` replaced
- [ ] `pip install fieldwork`: check the name is free on PyPI, or pick another before publishing
- [ ] Remove `FIELDWORK_ACCESS_PASSWORD` only when you want the site open
- [ ] Try company sign-in with a real Okta / Entra / Google account
- [ ] Connect a real Slack channel and one real GitHub repo, and watch a task sync both ways
- [ ] Connect Claude Code or Cursor with a personal token and ask it "what needs me today?"

## Signals worth tracking (from /api/operator/metrics)
- Active workspaces each week, and whether they're still active 4–8 weeks later
- Engine runs per workspace per week
- Workspaces with their own engines registered (the strongest sign of commitment)
- Requests for SSO, self-hosting or a security packet (budget showing up)
