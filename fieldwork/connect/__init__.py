"""Live connections to the tools a deployment team already uses.

core.py      installs, tokens, the inbound event store, the scheduler, health
routes.py    the HTTP surface (catalog, OAuth, webhooks, replay, live stream)
http.py      outbound calls (guarded; replaced by a fake router in tests)
actas.py     run a console action as a person (Slack buttons)
billing.py   the billing bridge: sign-off → ERP, invoiced / paid → back
<provider>   one module per family of services (erp.py: NetSuite, Certinia, Oracle, SAP, Workday)
"""

from . import core, slack, trackers  # noqa: F401  (registers providers)
from . import crm, timesheets, calendars, mail  # noqa: F401
from . import billing, erp  # noqa: F401  (the billing bridge and ERP connectors)

REGISTRY = core.REGISTRY


def register(app, d) -> None:
    from fastapi.responses import JSONResponse

    from . import actas, routes
    app.add_exception_handler(core.ConnectError, lambda _r, e: JSONResponse({"detail": str(e)}, status_code=422))
    actas.bind(app, d)
    routes.register(app, d)
    crm.register_routes(app, d)
    mail.register_routes(app, d)
    calendars.register_routes(app, d)
    billing.register_routes(app, d)
