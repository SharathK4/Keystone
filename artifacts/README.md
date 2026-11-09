# Canonical artifacts — immutable

Two artifacts are committed because the hosted demo serves from them and the
regression anchors are defined by them. **Never rebuild or edit them in place.**

| Artifact | Path | Content hash |
| --- | --- | --- |
| Analytical snapshot `snap-a4c99d5a49a64ffd` (dataset `synth-eab9b3bbb256`, seed 2025, scale small, 7 scenarios) | `snapshots/snap-a4c99d5a49a64ffd/` | `668c4a7b6d6fb71c…` |
| Contagion model `discrete_hazard-9d1ffe99e9` | `contagion/` | `d99a0b97feb42050…` |

They cannot be reproduced byte-for-byte by a rebuild: the snapshot's content
hash covers its `created_at` timestamp and identifiers generated at build time.
A rebuild produces a *different, equally valid* snapshot with a new id. That is
why these copies are kept as files rather than as build instructions.

Everything else under `artifacts/` is ignored and may be regenerated freely.

`discovery/hall_of_fame.json` is the one mutable file kept here: the best rule a
language model has written in the rule lab for this snapshot, as text only. The
server never reads a score from it; it replays the rule on the default test at
every start, and a lab run that finds a better rule rewrites it.
