# Panel tester initial balance

**Status:** Implemented

## Goal

Cards 3, 6, and 7 on Strategies and DD5 expose `Стартовый баланс теста`.
The selected value is rendered as numeric `InitialBalance` in the native
tester `config_tester.json` for that job.

## Contract

- The field defaults to the canonical MRS3 tester-template value (`1000`).
- A request supplies `initial_balance` as a strictly positive finite number.
  Browser validation is advisory; the controller validates authoritatively.
- The ordinary tester, global FINALIST RETEST, and CHECK & RETEST pass the
  same value to the sole existing SINGLE_MODE config renderer.
- The active job manifest records the value, so retries use the original
  config.  Existing API callers that omit the field retain the template value.
- The value changes neither selected strategies nor PerformanceDB facts before
  the tester produces a report and the existing import validates it.

## Acceptance evidence

- Focused tests prove a valid request writes `InitialBalance` and invalid input
  is rejected before a tester starts.
- Static UI tests prove each of cards 3, 6, and 7 has the field and sends it
  only to its tester-start request.
