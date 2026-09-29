# Live connections

Fieldwork connects to the tools a deployment team already uses. Installs are
one click for the person connecting. Changes arrive as they happen where the
tool can push them. Every connection is also reconciled on a schedule, so a
missed webhook costs minutes, never data.

| Tool | What it feeds | How changes arrive | Who connects |
|---|---|---|---|
| Slack | alerts with decision buttons, DMs on assignment, `/fieldwork` | buttons and commands are instant | an admin, once |
| GitHub Issues | two-way task sync, assignees | repo webhook, created for you; nightly reconcile | an admin, once |
| Linear | two-way task sync, assignees | the app's webhook; nightly reconcile | an admin, once |
| Jira | two-way task sync, assignees | project webhook, created and renewed for you; nightly reconcile | an admin, once |
| Salesforce | the pipeline | polled every 15 minutes; nightly reconcile | an admin, once |
| HubSpot | the pipeline | signed webhooks, then a sync; nightly reconcile | an admin, once |
| Harvest | time | polled every 30 minutes; nightly reconcile of the last 60 days | an admin, once |
| Toggl Track | time | polled every 30 minutes; nightly reconcile | an admin pastes an API token |
| Google Calendar | time off, against capacity | push channel; hourly poll | each person |
| Outlook Calendar | time off, against capacity | Graph subscription; hourly poll | each person |
| Gmail | last customer contact (opt-in) | polled every 30 minutes | each person who opts in |
| Outlook Mail | last customer contact (opt-in) | polled every 30 minutes | each person who opts in |

CSV import stays for everything else (a PSA, a CRM with no connector).

## What happens to what arrives

1. **Verify.** Every delivery is checked against the provider's signature
   (Slack v0 HMAC, GitHub `X-Hub-Signature-256`, Linear's signing secret,
   Jira's signed token, HubSpot v3, Google channel token, Graph `clientState`).
   Anything unsigned or stale is refused with 401.
2. **Store.** The delivery is written to `inbound_events` keyed by the
   provider's own delivery id, so a retried delivery is stored once.
3. **Apply.** The change is applied through the same code the console uses,
   and audited under `tracker:<provider>` or `sync:<provider>`. If applying
   fails, the event stays pending and is retried with backoff. Anyone who can
   manage the connection can replay an event from the console.
4. **Reconcile.** On a schedule, each connection re-reads the source of truth
   and fixes any drift.

Every connection shows its health: last event, last sync, last reconcile,
pending and failed deliveries, and the last error. After a failure a
connection backs off (2 minutes, doubling, up to 6 hours). A provider that
refuses a refresh token marks the connection *reconnect*.

Tokens are encrypted at rest with the workspace's Fernet keys and refreshed
before they expire (or on a 401 for services that don't say when they expire).

## Privacy

- **Calendars** keep only time off: Google "Out of office" events, Outlook
  events shown as Away, and all-day events titled like time off (OOO, PTO,
  vacation, holiday, leave). No other event is stored.
- **Email** is opt-in per person and reads headers only (Gmail's
  `gmail.metadata` scope, Graph's `Mail.ReadBasic`). For each message to or
  from a customer's domain it keeps the customer, the direction and the time.
  It never keeps subjects, bodies or addresses. Disconnecting deletes what it
  collected.
- Customer domains come from the customer's settings
  (`PUT /api/customers/{id}/domains`) and from the email domains of that
  customer's own people in the workspace. Free-mail domains are ignored.

## Setting up the apps (once per install of Fieldwork)

Each provider needs an app registered once, by whoever runs Fieldwork. Put
its client ID and secret in the environment and restart. The Integrations
page shows exactly which variables are missing and the callback URL to use.

Everywhere below, `https://your-site` is `FIELDWORK_PUBLIC_URL`. The callback
(redirect) URL for every provider is:

```
https://your-site/oauth/callback
```

### Slack
Fastest: api.slack.com/apps → Create New App → From a manifest, and paste `docs/slack-app-manifest.yml` with your URL filled in; then set the three variables in step 6. By hand instead:

1. api.slack.com/apps → Create New App → From scratch.
2. OAuth & Permissions: add the redirect URL. Bot token scopes:
   `chat:write`, `chat:write.public`, `commands`, `incoming-webhook`,
   `users:read`, `users:read.email`, `im:write`.
3. Interactivity & Shortcuts: on, Request URL `https://your-site/hooks/slack/interact`.
4. Slash Commands: `/fieldwork`, Request URL `https://your-site/hooks/slack/command`.
5. Event Subscriptions: on, Request URL `https://your-site/hooks/slack/events`,
   bot events `app_uninstalled` and `tokens_revoked`.
6. Set `FIELDWORK_SLACK_CLIENT_ID`, `FIELDWORK_SLACK_CLIENT_SECRET`,
   `FIELDWORK_SLACK_SIGNING_SECRET` (Basic Information → App Credentials).

People are matched to Fieldwork by their confirmed Slack email (re-checked at
least hourly). A button runs the same route the console calls, as that person,
so their permissions apply. One Slack workspace connects to one Fieldwork
workspace. If your workspace requires company sign-in, note that Slack buttons
still work: Slack has already verified who pressed them.

### GitHub
1. GitHub → Settings → Developer settings → OAuth Apps → New OAuth App.
   Authorization callback URL: the callback above.
2. Set `FIELDWORK_GITHUB_CLIENT_ID`, `FIELDWORK_GITHUB_CLIENT_SECRET`.

Scopes requested: `repo`, `admin:repo_hook` (to create the webhook on repos
you link), `read:user`, `user:email`. GitHub Enterprise Server: set the API
base in Integrations.

### Linear
1. Linear → Settings → API → OAuth applications → New. Callback URL: the callback above.
2. In the same application, turn on the webhook for **Issues** with URL
   `https://your-site/hooks/linear/app`.
3. Set `FIELDWORK_LINEAR_CLIENT_ID`, `FIELDWORK_LINEAR_CLIENT_SECRET`, and
   `FIELDWORK_LINEAR_WEBHOOK_SECRET` (the webhook's signing secret).

### Jira (Atlassian Cloud)
1. developer.atlassian.com → Developer console → Create → OAuth 2.0 integration.
2. Authorization: callback URL above. Permissions → Jira API: `read:jira-work`,
   `write:jira-work`, `read:jira-user`, `manage:jira-webhook`.
3. Set `FIELDWORK_JIRA_CLIENT_ID`, `FIELDWORK_JIRA_CLIENT_SECRET`.

Jira expires webhooks registered this way after 30 days; Fieldwork refreshes
them every 25, and registers them again if one lapsed. An OAuth app gets five
webhooks per user per site, so Fieldwork keeps a single webhook filtered to
every linked project.

### Salesforce
1. Setup → App Manager → New External Client App (or Connected App). Enable
   OAuth, callback URL above, scopes `api`, `refresh_token, offline_access`.
2. Set `FIELDWORK_SALESFORCE_CLIENT_ID` (consumer key) and
   `FIELDWORK_SALESFORCE_CLIENT_SECRET`. For a sandbox, also
   `FIELDWORK_SALESFORCE_LOGIN_URL=https://test.salesforce.com`.

If you track expected weekly delivery hours on the Opportunity, name the field
in the connection's settings (for example `Weekly_Hours__c`).

### HubSpot
1. developers.hubspot.com → create an app (Fieldwork uses HubSpot's OAuth v3 endpoints). Auth: redirect URL above, scopes
   `oauth`, `crm.objects.deals.read`, `crm.objects.companies.read`.
2. Webhooks: target URL `https://your-site/hooks/hubspot/app`; subscribe to
   `deal.creation` and `deal.propertyChange` for `dealstage`, `amount`, `closedate`.
3. Set `FIELDWORK_HUBSPOT_CLIENT_ID`, `FIELDWORK_HUBSPOT_CLIENT_SECRET`.

### Harvest
1. id.getharvest.com/developers → Create New OAuth2 Application, redirect URL above.
2. Set `FIELDWORK_HARVEST_CLIENT_ID`, `FIELDWORK_HARVEST_CLIENT_SECRET`.

### Toggl Track
Nothing to register. A workspace admin pastes their API token (Profile
settings → API Token) in Integrations. Admin rights let Fieldwork read
everyone's time through the Reports API.

### Google (Calendar and Gmail)
1. console.cloud.google.com → APIs & Services: enable the Google Calendar
   API (and the Gmail API for the email signal).
2. Credentials → Create OAuth client ID → Web application, redirect URI above.
3. OAuth consent screen: add the scopes `openid`, `email`,
   `.../auth/calendar.events.readonly` and, for email, `.../auth/gmail.metadata`.
4. Set `FIELDWORK_GOOGLE_CLIENT_ID`, `FIELDWORK_GOOGLE_CLIENT_SECRET`.

`gmail.metadata` is a restricted scope: until Google verifies the app, only
people in your own Google Workspace (an "Internal" app) can connect it.
Google pushes calendar changes only to https addresses.

### Microsoft 365 (Outlook Calendar and Mail)
1. entra.microsoft.com → App registrations → New registration. Redirect URI
   (Web): the callback above. Supported accounts: your choice.
2. Certificates & secrets → New client secret.
3. API permissions (delegated, Microsoft Graph): `offline_access`, `User.Read`,
   `Calendars.Read` (Graph change notifications on events need it), and `Mail.ReadBasic` for the email signal.
4. Set `FIELDWORK_MICROSOFT_CLIENT_ID`, `FIELDWORK_MICROSOFT_CLIENT_SECRET`,
   and optionally `FIELDWORK_MICROSOFT_TENANT` (default `common`).

## ERPs: the billing bridge

A statement of work in Fieldwork can be linked to a project or contract in one ERP. When the customer signs off a
milestone, the push is queued in the outbox in the same transaction (retried with backoff if the ERP is down, and
retryable by hand from the milestone). Every hour the connection reads invoiced and paid back. You can also import a
statement of work straight from the ERP project, which links both ways.

| ERP | Connect with | Sign-off does | Comes back |
| --- | --- | --- | --- |
| NetSuite | OAuth 2.0 with PKCE, using an integration record in your own account (account ID, client ID, secret) | Completes the milestone's project task, so its milestone billing line can bill | Invoice matched by amount after the sign-off; paid when nothing is unpaid |
| Certinia PSA | The Salesforce app | Status Approved and actual date; ticks Approved, Include In Financials, Approved for Billing where your org has them (settings) | Invoiced |
| Oracle Fusion Cloud | Integration user over REST (host, user, password) | Creates a project billing event (our milestone id in SourceReference), or dates and releases a planned one | Invoiced |
| SAP S/4HANA Cloud | Communication user (SAP_COM_0308 and billing documents) | Sets the milestone element's actual finish (CSRF token fetched first), which lifts the billing block on its plan date | Billing document; paid when cleared |
| Workday | A custom report (RaaS) shared with an integration system user | Nothing written: point a signed `milestone.accepted` webhook at your Workday integration | Invoice and payment status from the report (column names are settings) |

Details the vendors leave to each customer are settings, not guesses baked in: NetSuite's status id for a
completed task (default `COMPLETE`), Oracle's billing event type and contract line, Certinia's approval fields, and
Workday's report columns. If a value is wrong, the ERP's own answer shows on the milestone and on the connection.

No ERP connected? The finance CSV and the `milestone.accepted` webhook carry the same facts: amount, sign-off,
who and when, the evidence fingerprint and the record entry.

## Scheduling

The web process runs the scheduler: it applies pending events, polls,
reconciles and renews webhooks every 30 seconds, and immediately when a
webhook nudges it. `FIELDWORK_DIGEST_HOUR_UTC=12` sends the Slack digest once
a day at that hour from the same process, so no separate cron job is needed.

On a host that sleeps idle services (Render's free plan), nothing runs while
the service is asleep: the first webhook wakes it, and the reconcile catches
up on anything missed. Slack expects an answer within 3 seconds, so a
sleeping service will miss the first button press after a quiet spell. Use an
always-on plan for a real workspace.

## Honest status

Every connector here is built against the vendor's published API and tested
against a stand-in that answers the way that API documents. None has yet been
run against the live service. The first real connection per vendor is where a
rough edge would show; when one does, the connection's health says what the
vendor answered.
