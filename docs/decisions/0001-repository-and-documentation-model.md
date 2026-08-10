# ADR-0001: Root v0.7 repository and layered documentation

**Date:** 2026-08-10  
**Status:** Accepted

## Context

The imported project contained working v0.6 code under `programs/MRS3_v0.6`, repeated handoffs, and no Git repository. v0.6 is a starting point, not the active product version. New sessions must find current intent without loading all historical material.

## Decision

The repository root owns the current `src/mrs3`, `tests`, `scripts`, configuration, and public README. Active requirements live in `docs/specs`; historical v0.6 documents live in `docs/archive/v0.6`; `PRD.md` indexes active specs; `progress.md` reports only current operational state.

The local tester root is stored only in ignored `config.local.json`. Every commit requires a separate-agent review and proportional verification before commit. This decision implements [PRD](../../PRD.md) and affects [repository foundation](../specs/2026-08-10-mrs3-v07-repository-foundation.md) and [v0.7 legacy selection](../specs/2026-08-10-v07-legacy-selection.md).

## Consequences

New v0.7 work no longer inherits the misleading `MRS3_v0.6` path. Historic algorithm details remain accessible by explicit links. The repository can be shared without leaking local paths or large raw data.
