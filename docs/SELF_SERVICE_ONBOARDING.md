# Self-service onboarding v1

The customer experience is **get access → describe delivery → import active work → review → apply → use Fieldwork**. No consulting or implementation contract is involved. Customer administrators authorize their own connections and invite their own team using the existing Integrations and Team screens.

## Included in this version

- A Get started screen in the existing console and a shortcut on the new-workspace home screen.
- Optional AI proposals for stage labels and spreadsheet column mapping, using the operator's centrally configured model access. Customers do not need their own AI account or key.
- A fully usable guided path when AI is unavailable or the customer chooses not to send a description/headings to a model.
- CSV upload/paste, synonym mapping, full-row review, visible defaults/ignored columns, actionable errors and up to 200 deployments per preview.
- Explicit customer review, a 30-minute preview expiry, actor/tenant binding and rejection of stale settings or existing deployments.
- Atomic setup activation and idempotent repeated activation. Existing roles, permissions, engines, stage keys, SSO and connected tools are preserved. Imported deployments are initially led by the reviewing administrator.
- Audit entries that fingerprint the reviewed plan. Model outputs cannot execute tools or grant access.

AI is opt-in for each preview. Only the entered description and spreadsheet headings are sent to OpenAI, with `store: false`; CSV row contents stay in Fieldwork. Customers should still avoid entering sensitive information in descriptions or column headings. Provider failure leaves workspace delivery/configuration unchanged and the guided path available. Drafts contain the normalized import rows and expire after 30 minutes; expired drafts are deleted during subsequent previews in that tenant.

## Operator configuration

Set these server environment variables to enable AI:

```
FIELDWORK_ONBOARDING_OPENAI_API_KEY=<server-managed credential>
FIELDWORK_ONBOARDING_MODEL=<Responses API model supporting structured outputs>
```

Do not put credentials in the browser. The provider endpoint is fixed to `https://api.openai.com/v1/responses`, redirects are refused, requests time out and responses are size capped. No live model request is needed for deterministic onboarding or tests.

The implementation follows the [OpenAI structured-output contract](https://developers.openai.com/api/docs/guides/structured-outputs); model suggestions are also validated locally before a preview becomes activatable.

## Boundaries and next steps

This is the first functional onboarding slice, not proof that every enterprise can onboard unattended. This version changes labels on the existing stage chain; changing the number/order of stages, engines, criteria, roles and views remains available through Settings. It imports deployments, not historical tasks, SOWs or staffing. It does not automatically invite anyone, connect third-party tools, send messages or change billing.

Hosted payment checkout, verified subscription events, workspace entitlements, renewal/cancellation handling and automatic paid-workspace provisioning are not added by this change. A successful browser return from a checkout page must never count as payment verification. Those pieces are required for the complete **pay for a license → immediate access** flow. No prices or payment links in this version imply that billing is live.

Before release, validate live model output quality against realistic onboarding inputs, test real vendor OAuth connections and exercise signup-to-first-value with new customers. The provider contract tests use an HTTP stand-in; PostgreSQL code paths follow the database adapter but require a configured PostgreSQL test database to verify.

## Validation

Run `python -m pytest -q tests/test_onboarding.py` for permissions, non-destructive preview, replay, stale/expired plans, invalid imports, safe model mappings, provider payload minimization, transactional rollback and demo reset. Run the full suite before merging. The browser console script must pass JavaScript syntax validation as well.
