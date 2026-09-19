# reports/

Offline evaluation output. Nothing here is generated in CI and nothing but this
README is committed.

- `tabicl-eval/<timestamp>.json` + `.md` — `python scripts/eval_tabicl_stack.py`,
  the walk-forward comparison of TabICL v2 in-context inference against the
  Situate stack's cross-sectional ridge on the same harness metrics (OOS IC with
  its bootstrap CI, deflated Sharpe, publish gates). See the README section
  "Tabular model (TabICL v2)".
