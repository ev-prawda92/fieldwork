# Vendored engines

Engine cores are copied unchanged from their standalone repos. Fieldwork talks
to them only through `fieldwork/engines/__init__.py`, so each repo keeps
evolving on its own and gets re-vendored when it changes.

| Engine | Source | Commit | Files |
|---|---|---|---|
| Sendero | github.com/ev-prawda92/sendero | fcf30a2 | backend/sendero_engine/classify.py → engines/sendero_core/classify.py |
| Threshold | github.com/ev-prawda92/threshold | fa3d4f1 | threshold/{classify,parse,profile,score}.py → engines/threshold_core/ |

The audit hash chain follows Arbiter's construction (github.com/ev-prawda92/arbiter),
reimplemented per tenant in fieldwork/audit.py.
