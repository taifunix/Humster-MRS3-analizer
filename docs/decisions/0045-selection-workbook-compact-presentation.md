# ADR-0045: Compact selection workbook without losing review compatibility

Status: Accepted (2026-09-26)

## Context

The Pareto selection workbook exposes duplicate and internal diagnostic columns.
Operators need a shorter, ordered review surface, but the same workbook is also
the input to full selection-review and RETEST-only import. The import contract
requires stable identity and analog fields and formerly required `RETEST`
immediately after `User Status`.

## Decision

New exports omit redundant/diagnostic presentation columns and place the equity
block and editable review columns in the requested order. The internal finalist
decision and snapshot traces are unchanged. `Auto Analog Of ID` and `Analog Of
ID` stay in the workbook as hidden columns so existing automatic and manually
edited analog decisions remain possible. `Причина` is the last physical column.

The importer accepts `RETEST` directly after either `User Status` (old books)
or `User Rank` (new books). All protected headers, values, and automatic-field
checks remain required. The workbook schema version stays `1`; old files are
not migrated or rewritten. The combined finalist-retest control validator no
longer requires the unused `Final` display header; its candidate identity,
status, rank, analog, and immutable snapshot checks remain in force.

## Consequences

Operators see a compact workbook but must unhide `Analog Of ID` to create or
retarget a manual `ANALOG`. Existing review/RETEST files remain importable.
Removed display-only columns remain available in the immutable selection
snapshot or source facts, not in the XLSX review surface.
