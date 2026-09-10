# ADR-0034: Portfolio Optimizer PRETEST_PROXY search and sizing

Status: accepted for the fixture and read-only Stage-1 boundary.

Stage 1 evaluates bounded PRETEST_PROXY compositions from the exact current
`FINALIST` rows. It uses source PerformanceDB equity paths and direct linear
scaling to the actual composition size. The result is preliminary evidence;
joint tick-test metrics and recommendation fields remain `UNKNOWN` or
`NOT_TESTED`.

The selected universe is the current user-ranked finalist rows after the
explicit launch symbol and direction maxima. Individual drawdown and ranking
`top_n` fields remain accepted for compatibility but do not remove Stage-1
rows. Every usable symbol contributes its nonempty LONG, SHORT, and
LONG+SHORT options. Singleton and two-symbol compositions are mandatory and
consume one evaluation budget unit each; larger cardinalities use the same
unique-composition budget. The configured `max_enumerated_combinations` is
the unique composition evaluation budget. A budget below the mandatory cost
fails before evaluation and reports the required and suggested (125 percent)
budgets.

Each symbol has one total full-position capacity shared by all selected
members. Initial member shares are equal, exchange quantities are rounded
down, and residual quantity steps are assigned in canonical member order.
Uniform risk and margin scaling is applied after full-cap proxy evaluation;
one post-round ratio correction is allowed. An unsatisfied corrected
constraint consumes its evaluation unit and is reported as
`POST_ROUND_CONSTRAINT_UNSATISFIED`.

Current-result equity rows are sparse observations rather than an expected
daily sample. The common-period resolver carries the latest observation at or
before each UTC boundary through the terminal boundary. A prior observation or
positive initial balance is required to seed the first boundary. Coverage and
gap values remain diagnostics and do not gate the current-result universe.

The built-in PRETEST_PROXY evaluator can use a bounded process pool whose width
is read from the existing machine-wide `duckdb_import.workers` setting. Worker
count is scheduling-only and does not enter Campaign identity or the Portfolio
settings document. The parent commits results in canonical task order and
retains only compact search facts; full source paths are re-evaluated for the
selected shortlist. Custom evaluators remain serial.

The decision supersedes ADR-0033 only for the individual drawdown gate,
independent full caps, exhaustive singleton/two-symbol enumeration,
`top_n` compatibility semantics, `max_candidates` output semantics, and
Stage-1 metric labels. It does not authorize a tester, runtime, trading, or
recommendation-ready result.
