# Panel Analysis Profile Design

## Goal

Replace the misleading static Panel analysis profile with a typed, human-readable
editor for exactly the configuration that changes fresh Source v6 analysis of
an already published surface.

## Boundary

The editor reads and atomically updates only the approved analysis whitelist in
`config.local.json`. All captions, help text and validation messages are Russian;
the JSON keys remain an implementation detail and retain their existing names.

### Included fields

| Group | Config values |
| --- | --- |
| Eligibility of points | `history_min_days`, `base_rate_tf`, `shift_factors`, absolute trade floors, `event_filter.min_point_events` |
| Economic filters | `economic_min_pnl_pct`, `economic_min_win_rate_pct`, `economic_max_dd_pct`, `economic_min_efficiency` |
| Analysis geometry | `canonical_shifts_bp`, `refine.ma_neighbor_radius` |
| Plateaus and Close MA | plateau core/envelope/supported/equivalence/isolated-peak thresholds; `close_support.core_min`, `close_support.supported_min` |
| READY admission | `base_one_order` points/events/slots; `multi_order_admission` points/events |
| Multi-order structures | `gap_rules`, `max_orders`, `target_dd` |
| Performance | `duckdb_import.workers`, explicitly labeled as shared parallelism for Source import, publication and analysis |

The card also owns one operational input used by fresh analysis:
`panel_workflow.listing_dates_path`. It is displayed as a path with a native
file chooser and is saved atomically with the typed profile. The selected file
may be the legacy two-column listing-date CSV/XLSX or the operator liquidity
registry XLSX whose `Пары` sheet contains `Пара` and
`Дата листинга на Bybit (UTC)`.

### Excluded fields

Column mappings, materialization-only refine controls, shift-domain validation,
lot rounding, initial lots, and close multipliers do not affect the fresh
analysis of an existing surface and remain config-file-only.

## UI

The `Settings > Analysis profile` card is one main collapsible card with no
nested accordions. Its form uses Russian-labeled sections and visual separators
in the order above. Repeatable values use editable rows with explicit units
(percent, bp, events/month, processes). The card has only:

- **Reload**: discard unsaved browser edits and fetch the server's current
  profile; it never writes configuration.
- **Save**: send the typed profile; show field-level safe validation errors on
  failure and a saved confirmation on success.

Import batch size and unrelated existing controls are removed from this card.
The listing-date path is the only filesystem path exposed here because fresh
analysis cannot run without it.

## Server contract

Add a dedicated local-only analysis-profile endpoint rather than expanding the
generic Panel path-defaults payload. The endpoint:

1. reads current JSON while retaining unknown/unrelated keys;
2. accepts only the whitelist schema;
3. merges only the whitelist values;
4. validates the merged document through `AlgorithmConfig.from_json`;
5. validates that the listing-date path names a readable supported file;
6. atomically writes the complete local config, including the updated
   `panel_workflow`, only after both validations; and
7. returns a non-sensitive profile projection plus the configured path.

Workers validate as a positive integer under the existing import settings
contract. No other filesystem path or secret is accepted by this endpoint.

## Invariants

- Changing a profile value does not mutate an existing analysis artifact.
- The next fresh analysis reloads the config and receives a distinct config hash
  when a result-affecting value changes.
- Invalid browser input never partially changes the configuration.
- A missing, locked, malformed or duplicate-symbol listing-date file produces
  a distinct actionable error and never writes the configuration.
- The profile cannot overwrite import paths, runner settings, credentials,
  templates, or any unlisted configuration value.

## Verification

- Unit tests prove field projection, whitelist rejection, merge preservation,
  `AlgorithmConfig` validation and atomic-write failure safety.
- HTTP tests cover reload/save and path-free validation failures.
- Static UI tests prove Russian labels, group coverage, reload and save wiring.
- A live panel smoke reloads the local profile, saves an unchanged profile, and
  starts a fresh analysis with the displayed configuration.
