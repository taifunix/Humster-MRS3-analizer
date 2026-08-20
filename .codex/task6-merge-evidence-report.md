# Stage 1 Task 6 production partition-merge evidence

Date: 2026-08-20
Status: **CORE MERGE PASS; recovery boundary recorded separately**

This report covers the real `Input/HTML` 684-report corpus only. Generated
partition databases and merge targets were isolated at:

```text
C:\Users\PYTHON~1\AppData\Local\Temp\8\mrs3-task6-merge-20260820-gcug7v5d\
```

No source code, specification, progress file, or raw HTML was edited. Stage 2
was not run.

## Raw corpus and partition imports

The raw-corpus composite digest uses sorted relative POSIX paths, byte sizes,
per-file SHA-256 values, NUL separators, and an aggregate SHA-256. The corpus
was unchanged from the accepted Task 5 run:

```text
root:          Input/HTML
files:         684 before, 684 after
bytes:         285794916 before, 285794916 after
digest before: efe05916fafb234242aa0dcb0360fa5f3826bcc821bb80470dbe0a2455457192
digest after:  efe05916fafb234242aa0dcb0360fa5f3826bcc821bb80470dbe0a2455457192
```

Each partition was created as a generated copy of the raw reports and imported
with the accepted `preflight_source_v6` + `import_source_v6` API, workers=8,
batch_size=32. Every import used one parent writer, committed all reports,
quarantined none, and returned `safe_to_delete=YES`.

| partition | reports | database | source_content_digest | elapsed seconds |
| --- | ---: | --- | --- | ---: |
| 342a | 342 | `342a.source-v6.duckdb` | `7d7e044a5174ba40d6176e1753a7d6a5a3c615b9f47cee780597aab0725749bd` | 136.836 |
| 342b | 342 | `342b.source-v6.duckdb` | `0f1fb9c38d10cc30ddbb80bf0639bff80d0ef91622fb0c5be0f9ad6c90c110a7` | 85.277 |
| 228a | 228 | `228a.source-v6.duckdb` | `c2aaa88a5cad3309415d3edae58f2118b46c0c0d7d294b1b2bfca8dfc01ce4ec` | 86.768 |
| 228b | 228 | `228b.source-v6.duckdb` | `09f733d1cd12b8862294eafeb1673ba6c3c4e4463474d5fb1328f5bb0b699a17` | 72.550 |
| 228c | 228 | `228c.source-v6.duckdb` | `6e71e40027518dc4e2977815381b60597fc564f5163b2d8ec5ba7bb63690bb5dd` | 45.960 |

The corresponding raw partition roots are `partitions/342a`, `partitions/342b`,
`partitions/228a`, `partitions/228b`, and `partitions/228c` below the generated
temporary root.

## Merge permutations

All rows below came from `merge_source_v6`; every result reported
`writer_count=1`, `status=COMMITTED`, and zero quarantine. The full 684-fragment
merge digest is:

```text
1fc26d0d93b4a61bfe7323cbeedc0e79c841f9c7398facf10e6908269e2e700e
```

| output | inputs | accepted | duplicates | digest | elapsed seconds |
| --- | --- | ---: | ---: | --- | ---: |
| `342-ab` | 342a + 342b | 684 | 0 | `1fc26d0d93b4a61bfe7323cbeedc0e79c841f9c7398facf10e6908269e2e700e` | 310.947 |
| `342-ba` | 342b + 342a | 684 | 0 | `1fc26d0d93b4a61bfe7323cbeedc0e79c841f9c7398facf10e6908269e2e700e` | 319.648 |
| `228-ab` | 228a + 228b | 456 | 0 | `0bca76ee64a61961ccb5a870b32638230964c7872ad09c8bb3592e1258757410` | 219.228 |
| `228-bc` | 228b + 228c | 456 | 0 | `cae079745bac5f7ede421bb55de8cae430a32b6e9669945f434113c7cfd1092d` | 161.118 |
| `228-left-assoc` | (`228-ab`) + 228c | 684 | 0 | `1fc26d0d93b4a61bfe7323cbeedc0e79c841f9c7398facf10e6908269e2e700e` | 311.052 |
| `228-right-assoc` | 228a + (`228-bc`) | 684 | 0 | `1fc26d0d93b4a61bfe7323cbeedc0e79c841f9c7398facf10e6908269e2e700e` | 311.544 |
| `228-idempotent` | 228a + byte-identical `228a-copy` | 228 | 228 | `c2aaa88a5cad3309415d3edae58f2118b46c0c0d7d294b1b2bfca8dfc01ce4ec` | 132.483 |

The duplicate-path probe `merge_source_v6((228a, 228a), ...)` is not counted as
idempotence evidence because the API canonicalizes repeated identical paths
before reading them (`duplicate_count=0`). The byte-identical second database
path is the accepted duplicate-content check.

For `342-ab` and `342-ba`, the decoded canonical-fragment semantic digest was:

```text
69d2088b4066282372f7acd6fd081e2ddada65edab718fc86c292931bf9332a0
```

The same canonical digest and 684 fragment count were obtained from the clean
Task 5 full import `run-w1.duckdb`. Thus `AB=BA=full` holds at canonical content
level, while the contract-level `source_content_digest` also matches all three
outputs. The 3-way associativity outputs both contain 684 active fragments and
the same full source-content digest, establishing `(A+B)+C = A+(B+C)` under the
canonical identity contract.

## Origins, writer and input safety

- `342-ab`, `342-ba`, and the clean full import each read back 684 compact
  fragments, 684 flattened origin rows, 4,337 day-ownership rows, and zero
  quarantine rows.
- The duplicate-content idempotence output retains the duplicate input origins
  as flat origin records; it does not store nested database lineage.
- Every importer and merge result recorded `writer_count=1`; no worker writes
  DuckDB.
- Merge inputs were opened read-only. The merge API checked each input's
  `(size, mtime_ns)` before and after read and again before publication; all
  successful merges passed these checks. No input `.wal` or `.tmp` sidecar was
  present at final inspection.
- The original interrupted merge left a generated staging DB but did not alter
  either input partition. Its recorded artifact was:

  ```text
  .342-ba.source-v6.duckdb.b022846fa466498aa55f6da5e84dbab4.staging
  size: 111161344 bytes
  sha256: C22B9212FA338EA5DFAE14EE1C849050EB95C1E44A5B7580E9C0A0F98AF32A53
  ```

  That generated orphan was removed, then a clean BA merge was rerun and
  committed with the same canonical and source-content digests as AB. No
  target-specific staging artifact remained after the retry.

The first interrupted-run harness did not retain a separate cryptographic
before/after matrix for every input sidecar; the evidence available here is the
merge API's read-only/stat guards, final sidecar inspection, and unchanged raw
corpus digest. No stronger hash claim is made.

## Recovery and serialized writes

Accepted importer stale-staging recovery, including resume-to-clean-digest,
zero quarantine, one writer and zero target-specific staging/WAL/TMP files, is
recorded separately in [task6-recovery-overlap-report.md](task6-recovery-overlap-report.md).
That run committed 228 rows with digest
`c2aaa88a5cad3309415d3edae58f2118b46c0c0d7d294b1b2bfca8dfc01ce4ec`.

The merge kill-before-commit path exposed an implementation boundary: unlike
the importer, `merge_source_v6` has no stale-staging recovery pass. The BA
staging artifact therefore required explicit generated-artifact cleanup before
the clean retry above. The merge kill-after-publish and Debian kill/recovery
matrix were not claimed; this Windows host has no installed WSL/Debian
distribution. Existing focused merge tests cover rejection of a held target
lock (`OutputDirectoryBusyError`) and existing-target refusal; no overlapping
write was allowed to publish a target.

## Separate overlap corpus

The user-provided `Input/my_test` corpus was kept separate and inspected
read-only. The detailed probe is in
[task6-recovery-overlap-report.md](task6-recovery-overlap-report.md): 684 files,
684 unique canonical points, zero same-point pairs, zero compatible overlap
pairs, and no qualifying `>=96h` overlap candidate. Consequently no overlap
stitch claim was made and the corpus was not mixed with the 684-report
partition merge evidence.

Generated temporary databases and partition copies remain available for review
and cleanup. The temporary runner was removed from the repository. No commit
was created.
