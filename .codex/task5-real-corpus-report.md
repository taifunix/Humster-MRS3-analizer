# Stage 1 Task 5 real-corpus evidence

Date: 2026-08-20
Status: **RERUN PASS вЂ” Stage1 Task5 evidence complete after accepted codec fix**

## Scope and input identity

- Input root: `Input/HTML` (read-only).
- Expected and observed file count: `684` HTML files.
- Total raw bytes: `285,794,916`.
- Raw-corpus digest algorithm: sort relative POSIX paths; for each file append
  `relative_path + NUL + byte_size + NUL + file_SHA256 + LF`; SHA-256 the
  resulting UTF-8 bytes.
- Digest before run:
  `efe05916fafb234242aa0dcb0360fa5f3826bcc821bb80470dbe0a2455457192`.
- Digest after failed run:
  `efe05916fafb234242aa0dcb0360fa5f3826bcc821bb80470dbe0a2455457192`.
- The raw corpus was not edited, moved, or deleted.

## Read-only compatibility probe

Command (local environment):

```text
.\.venv\Scripts\python.exe -c "from pathlib import Path; from mrs3.source_v6 import normalize_source_v6; p=Path(...first HTML...); f=normalize_source_v6(p.read_bytes(), source_name=p.name); print(f.point.canonical_key, f.fragment_id)"
```

Result: the first report normalised successfully:

```text
source = my_test_run_001_of_684_AAOIUSDT_2h_2026-07-30.html
point = AAOIUSDT|LONG|2h|30|SMA|ohlc4|2|SMA|ohlc4|2
fragment_id = fe2a7c99272caa9bc5ae96a5eed746f18d42fd035cef95152bb02a88e41c27e9
actions=141 cycles=71 events=141 wallet_samples=1858 equity_samples=1858
```

The failing report also normalised successfully before storage:

```text
source = my_test_run_037_of_684_AAOIUSDT_2h_2026-07-30.html
source_sha256 = 73b5f664fbc4d1b017b33652eb9435db663717e1ade83963bce8f0a84809b8e9
fragment_id = ffc78896cf7d6771cba8b3d3db98494ddee8dc0608ce8464143a9866dae43d9f
```

Therefore this is not an HTML parsing/normalisation compatibility failure.

## Import attempt and measurements

Generated artifacts were isolated under:
`%TEMP%\mrs3-task5-real-corpus-20260820\`.

Runner command (workers=1, codec default):

```text
.\.venv\Scripts\python.exe C:\Users\PythonScripts\AppData\Local\Temp\mrs3-task5-real-corpus-20260820-run-debug.py Input\HTML %TEMP%\mrs3-task5-real-corpus-20260820\failed-metrics.duckdb 1
```

Observed output:

```json
{"status":"FAILED","error":"SourceV6ImportError('compact fragment readback mismatch')","workers":1,"preflight_seconds":0.9870440999511629,"total_seconds":21.939135499997064,"rss_before":43163648,"rss_after":242409472,"db_exists":false,"db_bytes":0,"wal_exists":false,"wal_bytes":0,"staging_residuals":[]}
```

The failing input was report 037 while committing the second parent batch:

```text
SourceV6StorageError: compact fragment readback mismatch
```

The isolated diagnostic reconstructed the stored fragment and found the first
losslessness difference at action index 51:

```text
expected pnl    = Decimal('4.6530000000000000000000000117')
restored pnl    = Decimal('4.653000000000000000000000012')
expected balance= Decimal('1003.4315492950000000000000000')
restored balance= Decimal('1003.431549295')
```

This is consistent with the current canonical decimal path calling
`Decimal.normalize()` under the active precision context before storage; the
real report contains more precision than that context preserves. The importer
correctly refuses to publish a non-lossless fragment. Fixing this requires a
codec/code change and is outside this evidence-only task; no such change was
made.

## Gate checks not reached

Because the first worker run failed before atomic publication:

- workers `4` and `8` were not run; no arbitrary speedup claim is made;
- no committed Source DB exists from this run;
- exact `19 x 6 x 6 = 684` committed rows/cells was not established;
- reconstructed-fragment equality for all raw HTML was not established;
- source-content digest, audits, zero quarantine, and `safe_to_delete=YES`
  advisory evidence were not established;
- DB/WAL/staging final measurements are failure measurements only (all zero or
  absent after cleanup); peak parent RSS was 242,409,472 bytes;
- Debian bundled smoke was not run; Stage 1 is already blocked on the primary
  OS and no Debian environment was available/required;
- Stage 2 was not called and no `STAGE_1_GATE` token was added.

## Reversible cleanup state

The temporary directory and diagnostic DuckDB files were retained for review at
`%TEMP%\mrs3-task5-real-corpus-20260820\`. Failed target publication left no
`failed-metrics.duckdb`, WAL, or staging artifact. The retained files are
generated evidence only and can be removed later; raw HTML remains intact.

No repository source/spec/progress file was changed by this task. The worktree
already contained unrelated dirty/untracked changes when checked; `git diff
--check` reported no whitespace errors.

## Fresh rerun after accepted codec fix

The prior blocker was fixed and independently reviewed by the root as
`CODE_REVIEW_PASS`. A fresh run used a new temporary root:

```text
%TEMP%\mrs3-task5-real-corpus-20260820-rerun\
```

The first-report probe still normalised successfully (`AAOIUSDT|LONG|2h`,
141 actions, 71 cycles, 141 events, 1858 wallet samples, 1858 equity samples).
The previously failing report 037 also passed direct compact import/readback;
its new fragment id was
`22bdf937cf9368b3b439e42ecfee255f72b55918f86d030a0a7dc536bb3d8bee` and the
diagnostic reported `differences={}`.

Raw corpus was measured with the same deterministic algorithm before and after
the rerun (sorted relative POSIX path, byte size, per-file SHA-256, NUL
separators, aggregate SHA-256):

```text
count=684
total_bytes=285794916
before=efe05916fafb234242aa0dcb0360fa5f3826bcc821bb80470dbe0a2455457192
after =efe05916fafb234242aa0dcb0360fa5f3826bcc821bb80470dbe0a2455457192
```

### Primary-OS worker sweep (one repeat per setting)

| workers | preflight s | import s | total s | parent RSS before | parent RSS after | DB bytes | WAL bytes | accepted | committed audits | quarantine | safe_to_delete | writer |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 1 | 0.984929 | 375.681749 | 376.666678 | 42,987,520 | 1,026,420,736 | 123,219,968 | 0 | 684 | 684 | 0 | YES | 1 |
| 4 | 0.963426 | 304.799068 | 305.762494 | 42,659,840 | 1,030,098,944 | 122,433,536 | 0 | 684 | 684 | 0 | YES | 1 |
| 8 | 0.985543 | 299.334341 | 300.319884 | 42,934,272 | 1,014,788,096 | 122,957,824 | 0 | 684 | 684 | 0 | YES | 1 |

All three runs used the default codec, 22 parent batches (`21 x 32 + 12`),
and emitted the same source digest:

```text
1fc26d0d93b4a61bfe7323cbeedc0e79c841f9c7398facf10e6908269e2e700e
```

Each DB reported `schema_version=6`,
`fingerprint=source-v6-fresh-compact-v1`, `mutation_generation=684`, 684
compact rows, 684 distinct fragment/source identities, 684 active rows, 684
audits, 684 committed audits, zero non-committed audits, zero quarantine rows,
and 4,337 day-ownership rows. No WAL or staging residue remained after any
run. DB output paths were `run-w1.duckdb`, `run-w4.duckdb`, and `run-w8.duckdb`
under the temporary root above.

### Exact Cartesian cells and readback

The exact observed set was:

```text
scope: AAOIUSDT | LONG | 2h
shifts_bp: 30,40,50,60,70,90,110,140,170,200,230,270,310,350,390,430,470,510,550 (19)
OpenMA: 2,3,4,5,6,7 (6)
CloseMA: 2,3,4,5,6,7 (6)
```

Observed cells: `684`; expected cells: `684`; missing: `[]`; extra: `[]`.
The full verifier found `reconstructed_equals_iterated=true` for all three DBs.
For the workers=1 DB, every raw HTML was normalized and compared with its
reconstructed fragment: `684` matches, `0` mismatches. An independent source
SHA comparison also matched all `684/684` raw files to their stored origins.

The Debian bundled smoke was not run because no Debian environment was
available; the plan explicitly makes Debian smoke secondary to the primary-OS
real-corpus gate. No Stage2 operation or `STAGE_1_GATE` token was added.

The temporary rerun directory retains only generated probe/DB artifacts and
three importer lock directories for reversible cleanup. No raw HTML was
modified, moved, or deleted. No Task5 subprocess remains active.
