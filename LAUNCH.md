# Launch checklist

## 1. License: done
`LICENSE.md` is FSL-1.1-ALv2: free to use, change and self-host; nobody else can sell it as a competing service; each version becomes Apache 2.0 after two years.

## 2. Private GitHub repo: done
The code lives in a private repo. Render deploys from it once you connect GitHub in Render.

## 3. Deploy on Render

**Free demo (no card needed):** New → Blueprint, pick the repo, and set the Blueprint path to `render.free.yaml`. You get a public demo that seeds itself, resets hourly and sleeps when idle (about a minute to wake). Its data lives on the service's own disk and is wiped on restart, which is fine for a demo and not for real work.

**Always on (about $13/month: Starter web service + Basic Postgres):**
1. In Render: **New → Blueprint**, pick the repo. `render.yaml` creates the web service and a Postgres database. The Slack digest and every connector sync run inside the web service.
2. Generate an encryption key on your computer and paste it as `FIELDWORK_SECRET_KEYS`:
   `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   Keep a copy somewhere safe. Losing it means reconnecting every integration.
3. Set `FIELDWORK_ACCESS_PASSWORD` to keep the site private, and `FIELDWORK_PUBLIC_URL` to the site's https URL (webhooks and sign-in callbacks are built from it).
4. Deploy. The demo workspace seeds itself on first start and resets every 60 minutes.
5. Register the apps you want to connect ([docs/CONNECTIONS.md](docs/CONNECTIONS.md)) and add their `FIELDWORK_<PROVIDER>_CLIENT_ID` / `_CLIENT_SECRET` in Render's Environment tab. The Integrations page shows what's missing.
6. Usage numbers: `curl -H "X-Operator-Token: <FIELDWORK_OPERATOR_TOKEN from Render>" https://<site>/api/operator/metrics`

Free Postgres on Render expires after 30 days, so it isn't used here. For a real (non-demo) workspace, set `FIELDWORK_DEMO=0` and `FIELDWORK_DEMO_RESET_MINUTES=0`.

## 4. Before going public
- [x] License chosen (FSL-1.1-ALv2)
- [ ] `pip install fieldwork`: check the name is free on PyPI, or pick another before publishing
- [ ] Remove `FIELDWORK_ACCESS_PASSWORD` only when you want the site open
- [ ] Try company sign-in with a real Okta / Entra / Google account
- [ ] Install the Slack app and press a button on a real alert
- [ ] Connect one real GitHub repo, Linear team or Jira project, and watch a task and its assignee sync both ways
- [ ] Connect the CRM and a time tool, then check Pipeline and Team against what you know
- [ ] Connect your own calendar and check your week
- [ ] Connect Claude Code or Cursor with a personal token and ask it "what needs me today?"

## Signals worth tracking (from /api/operator/metrics)
- Active workspaces each week, and whether they're still active 4–8 weeks later
- Engine runs per workspace per week
- Workspaces with their own engines registered (the strongest sign of commitment)
- Requests for SSO, self-hosting or a security packet (budget showing up)
