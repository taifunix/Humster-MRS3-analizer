# Stage 1 Task 6 bounded recovery/overlap evidence

Date: 2026-08-20
Status: **RECOVERY PASS; cross-corpus overlap and representative stitch evidence complete**

## Scope and isolation

The recovery run used the existing temporary merge/evidence root. The overlap
probe compared the two source roots `Input/HTML` and `Input/my_test` read-only;
they were never mixed into the 684-fragment recovery corpus:

```text
C:\Users\PYTHON~1\AppData\Local\Temp\8\mrs3-task6-merge-20260820-gcug7v5d\
```

No source, test, or raw HTML file was edited. The compared roots were checked
read-only at:

```text
D:\SHARE\!MN\hamster\MRS-Analizer\Input\HTML
D:\SHARE\!MN\hamster\MRS-Analizer\Input\my_test
```

## Import recovery after interruption

The existing stale artifact was retained as the interruption fixture:

```text
stale staging = .recovery-import-before.source-v6.duckdb.bed24d3789274f2baa2fac0199209abd.staging
source root   = partitions/228a
clean oracle  = 228a.source-v6.duckdb
target       = recovery-import-before.source-v6.duckdb
```

Read-only state before resume:

```text
HTML files in source root: 228
target existed: false
stale staging existed: true
stale staging rows: 228
stale staging source_content_digest: c2aaa88a5cad3309415d3edae58f2118b46c0c0d7d294b1b2bfca8dfc01ce4ec
clean oracle rows: 228
clean oracle source_content_digest: c2aaa88a5cad3309415d3edae58f2118b46c0c0d7d294b1b2bfca8dfc01ce4ec
target-specific staging matches before: 1
```

The importer was resumed against the same 228 HTML files (`workers=2`,
`batch_size=32`). It recovered the stale staging path under the importer lock,
then completed a fresh atomic publication:

```json
{"status":"COMMITTED","accepted_count":228,"quarantined_count":0,"safe_to_delete":"YES","writer_count":1,"batch_sizes":[32,32,32,32,32,32,32,4],"elapsed_seconds":100.022,"source_content_digest":"c2aaa88a5cad3309415d3edae58f2118b46c0c0d7d294b1b2bfca8dfc01ce4ec"}
```

Read-only state after publication:

```text
target rows: 228
target distinct sources: 228
target active rows: 228
target import audits: 228 (committed: 228)
target quarantine rows: 0
schema_version: 6
fingerprint: source-v6-fresh-compact-v1
mutation_generation: 228
target source_content_digest: c2aaa88a5cad3309415d3edae58f2118b46c0c0d7d294b1b2bfca8dfc01ce4ec
target digest == clean oracle digest: true
target-specific staging matches after: 0
target-specific .wal matches after: 0
target-specific .tmp matches after: 0
stale staging exists after: false
```

Raw-input immutability was measured before and after using the sorted relative
HTML names and per-file SHA-256 values as a composite SHA-256:

```text
count before: 228
count after:  228
composite before: e718f4b8fe268f46c7f723c88dc337567a28f21cb0c6b046deba19413192f60f
composite after:  e718f4b8fe268f46c7f723c88dc337567a28f21cb0c6b046deba19413192f60f
```

This establishes stale-staging recovery/resume equivalence to the clean
digest, zero quarantine, and no target-specific orphan staging/WAL/TMP files.

## Cross-corpus normalization and overlap

Both corpora were normalized read-only and compared by canonical point key.
The raw digest algorithm is the sorted relative POSIX path, byte size, and
per-file SHA-256 composite used above.

```text
corpus       files  raw bytes    digest before                                                   digest after
Input/HTML     684  285,794,916  31ab6a29506b37c9d71470350d54d42af20bb1541cc31deaa69b41352973cf7e  same
Input/my_test  684  338,731,803  86ce7195d4d20a47ea6ff6265592547bb15c09c91c988895eea7fc37188059d8  same
```

The older Task5/merge report records a different historical `Input/HTML`
digest (`efe05916...`) because that report used literal backslash separators;
the canonical NUL/LF POSIX-path digest used here is `31ab6a...`. The current
value was identical before and after all normalization, copying, and
compact-import checks.

Normalization/header results:

```text
                         Input/HTML                    Input/my_test
normalized / failures    684 / 0                       684 / 0
header-set variants      1                             1
Report range header      2026-07-30 - 2026-08-05       2026-08-01 - 2026-08-07
stitchability             fixed-lot 684/684              fixed-lot 684/684
unique canonical points  684                           684
unique settings          684                           684
effective periods        144h:451, 168h:233            144h:195, 168h:489
```

The canonical-point join was exact: `684` common points, `0` only in
`Input/HTML`, and `0` only in `Input/my_test`. All `684/684` joined pairs had
the same execution compatibility fingerprint and were fixed-lot stitchable.
The older `Input/HTML` period starts at `2026-07-30T00:00:00Z`; the incoming
`Input/my_test` period starts at `2026-08-01T00:00:00Z`. Header interval overlap
was therefore:

```text
96 hours:  451 pairs
120 hours: 233 pairs
<96 hours: 0 pairs
compatible >=96h pairs: 684
```

### Closed-cycle duplication audit

The two inputs cannot be aggregated across their overlap. A read-only,
eight-process audit normalized every matched pair and compared each closed
cycle opened in the exact timestamp overlap by `(open_timestamp,
close_timestamp, realized_pnl, fees)`; report-local order and cycle IDs were
intentionally not used as identity fields.

```text
pairs audited:                 684
pairs with >=1 duplicate cycle: 681
old closed cycles in overlap:  2310
new closed cycles in overlap:  1968
identical closed cycles:       1121
old-only cycles:               1189
new-only cycles:                847
```

Thus a later policy may select either report as the single owner of the
overlap, but it must never sum both. This audit does not by itself select a
seam policy or establish that post-seam balance samples are safe after an
excluded cycle.

### Old-owned overlap metric verification

After ADR-0013 selected old-owned overlap, the same 684 pairs were normalized
read-only and evaluated by `resolve_ownership` plus `calculate_metrics` with
eight workers. No raw input or DuckDB artifact was changed.

```text
pairs:                        684
USE_OLD_WITH_SEAM_EXCLUSION:  684
two local period metrics:     684
local PnL = final - anchor:   684
reported DD% = max(period DD%): 684
reported PF = retained-action PF: 684
```

This verifies the real corpus accepts the old-owned rule and produces two
period-local metric records for every pair. Focused regression tests separately
verify that excluded incoming overlap cycles do not contribute PnL, PnL%, DD,
DD% or Profit Factor, and that a boundary-crossing cycle contributes wholly to
the new period.

The 168-hour endpoints reflect the existing v6 date-only terminal-sample rule.
They were measured from normalized timestamps, not inferred from filenames.

### Ownership and bridge constraints

In-memory `resolve_ownership(old=Input/HTML, incoming=Input/my_test)` produced:

```text
RESOLVED:                         19
UNRESOLVED / BRIDGE_NOT_COVERED: 665
```

All 19 resolved pairs had old open tails, but the incoming fragment supplied
the required closed bridge cycles; the resolver's selected bridge facts were
persistable. Example points:

```text
point                                        overlap  old tails  new tails  bridge cycles/actions/events/samples
AAOIUSDT|LONG|2h|30|SMA|ohlc4|3|SMA|ohlc4|2   120h     23         30         9 / 9 / 9 / 1839
AAOIUSDT|LONG|2h|30|SMA|ohlc4|7|SMA|ohlc4|2    96h      6         13         1 / 1 / 1 / 1289
```

The rejected representative has a valid 120-hour compatible overlap but fails
the open-tail bridge contract:

```text
point: AAOIUSDT|LONG|2h|110|SMA|ohlc4|2|SMA|ohlc4|2
old period: 2026-07-30T00:00:00Z .. 2026-08-06T00:00:00Z; open tails: 13
new period: 2026-08-01T00:00:00Z .. 2026-08-07T00:00:00Z; open tails: 18
overlap: 120h; resolver: UNRESOLVED / BRIDGE_NOT_COVERED
```

`BRIDGE_NOT_COVERED` is fail-closed: an incoming report is not activated when
an outgoing open tail cannot be bridged by the incoming closed cycle evidence.

### Separate compact two-source readback

Representative two-file imports were isolated under:

```text
C:\Users\PYTHON~1\AppData\Local\Temp\8\mrs3-task6-overlap-20260820\
```

The resolved representative used run `007` (shift 30, OpenMA 3, CloseMA 2);
the bridge-rejected representative used run `217` (shift 110, OpenMA 2,
CloseMA 2). Each temp import contained exactly one old and one incoming HTML
copy, and the original roots remained read-only.

```text
                         resolved pair                 bridge-rejected pair
import status            COMMITTED                     COMMITTED
accepted/quarantine     2 / 0                         2 / 0
safe_to_delete          YES                            YES
compact rows/readback   2 / 2                         2 / 2
active old/incoming     false / true                   true / false
incoming disposition    (active)                       BRIDGE_NOT_COVERED
audits committed        2                              2
quarantine rows         0                              0
fact ownership          3829 total / 3829 active       4078 total / 4078 active
day ownership           13 total / 6 active             13 total / 7 active
```

Resolved compact digest:
`0a65d278e8ab8627cacdea71962273b9c084070adb1ff3bd38672a8f8e113aa5`.
Bridge-rejected compact digest:
`a28bbc7ac29d5982eb79ebc67dad0e9020eca91461ecb89abf03689e0d3c5b3f`.
Both temp imports passed schema/readback validation and left no target-specific
staging, WAL, or TMP residuals. The original raw digests remained unchanged
after normalization, copying, and compact import.

### Bounded merge stale-staging recovery

Using the existing partition inputs `228a.source-v6.duckdb` and
`228b.source-v6.duckdb`, a target-specific stale artifact was created and left
in place before the merge call:

```text
target: recovery-merge.source-v6.duckdb (absent before run)
stale: .recovery-merge.source-v6.duckdb.stale-evidence.staging
stale rows: 228
stale source_content_digest: c2aaa88a5cad3309415d3edae58f2118b46c0c0d7d294b1b2bfca8dfc01ce4ec
stale DB SHA-256: 47623f71751c3a3452fb3655eaa174160b99108760b75eb2b3326d062c268938
stale .wal: present, 0 bytes, SHA-256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
stale .tmp: present, 0 bytes, SHA-256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
target-specific staging matches before recovery: 3
```

`merge_source_v6(228a, 228b, recovery-merge)` was invoked without manual
cleanup. The locked recovery pass removed all three stale artifacts before
building the new staging database:

```json
{"status":"COMMITTED","input_count":2,"accepted_count":456,"duplicate_count":0,"writer_count":1,"active_count":456,"elapsed_seconds":237.872,"source_content_digest":"0bca76ee64a61961ccb5a870b32638230964c7872ad09c8bb3592e1258757410"}
```

The target readback and clean oracle agreed:

```text
target rows / active: 456 / 456
target mutation_generation: 456
target source_content_digest: 0bca76ee64a61961ccb5a870b32638230964c7872ad09c8bb3592e1258757410
readback rows / digest: 456 / 0bca76ee64a61961ccb5a870b32638230964c7872ad09c8bb3592e1258757410
clean 228-ab rows / active: 456 / 456
clean 228-ab source_content_digest: 0bca76ee64a61961ccb5a870b32638230964c7872ad09c8bb3592e1258757410
digest equals clean oracle: true
```

The input DB plus sidecar identities were unchanged across the merge:

```text
input 228a DB: size 71,839,744; mtime_ns 1787228637905926900; content identity SHA 57b2487acb014393f5ac9d0e8a38164326f131586e84737578347a2ea324fe85; DB SHA 47623f71751c3a3452fb3655eaa174160b99108760b75eb2b3326d062c268938; .wal/.tmp absent before and after
input 228b DB: size 51,392,512; mtime_ns 1787228748808994600; content identity SHA 359fe1a400a9a43d65b606b634d099ceaf469656fff2eea8ade888dec5c714fd; DB SHA e4553cd5e53664ed70164bdc2a2be73681957d1dd71f31ec3cdfde56538b73ff; .wal/.tmp absent before and after
```

After publication, the stale DB/WAL/TMP and target-specific staging matches
were all absent; target `.wal` and `.tmp` were also absent. No merge
permutations were rerun.

No code, specification, progress, or raw-input file was changed. This report
was updated with the cross-corpus evidence; no commit was created.
