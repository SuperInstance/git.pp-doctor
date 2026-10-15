# git.pp-doctor

> A single-command validator for git.pp record sets. Pure-Python, stdlib only, byte-identical to the canonical referee for the operations it supports.

The intent is *not* to be the fold. The fold lives in [`patchwork-quilt/git.pp-ng`](https://github.com/patchwork-quilt/git.pp-ng) and its sibling ports. The intent is a no-setup CLI that answers four questions of a record set:

1. **Is this set well-formed?** (no orphan cycles, no missing parents)
2. **What is the closure root at `<at>`?** (SHA-256 over the pinned format-v2 leaf domain)
3. **Does it match a pinned root?** (`VERIFIED` / `STATE-ONLY` / `MISMATCH`)
4. **What is the derived state?** (the fold, replayed in ~150 lines of stdlib)

## Quick start

```bash
# Fetch a record set from a known project
curl -s https://gitpp.superinstance.dev/api/records?project=poc-quilt-1 | jq '.records' > poc-quilt-1.json

# Compute the closure root at task-c (the pinned project root)
python3 gitpp_doctor.py root poc-quilt-1.json --at task-c
# -> "60effe23970d8a5cc837636c44685502361288c1f00577e9addfa10cbd4fc91d"

# Three-way check against the pinned root
python3 gitpp_doctor.py check poc-quilt-1.json \
  --at task-c \
  --expected 60effe23970d8a5cc837636c44685502361288c1f00577e9addfa10cbd4fc91d
# -> {"verdict": "VERIFIED", ...}

# Replay the fold
python3 gitpp_doctor.py fold poc-quilt-1.json | jq .state
# -> {"config.timeout": 45, "env.oracle_disk_pct": 93, "feature.cache": "on", ...}
```

## What it covers

- **Format v2 leaf construction** (CHECKPOINT-V2.md §"Record leaf") — `id`, `parents`-as-set (sorted by Unicode code point), `kind`, `effects` (with float64 canonicalization + leaf-number encoding), `reason`. The leaf is the SHA-256 of `json.dumps(canon, sort_keys=True, ensure_ascii=True, separators=(", ", ": "))`.
- **Closure root** — SHA-256 of `"\n".join("id:leaf")` over the closure of `at`, sorted by Unicode code point.
- **The fold** — Kahn topo order with smallest-id-first, class precedence `override > task > witness`, ancestry reduction, cleanest-chain tie-break, conflict losers preserved.
- **Three-way verdict** — `VERIFIED` (root matches), `STATE-ONLY` (root differs, state unchanged), `MISMATCH` (state differs). The shrunk/swapped-vs-grown classification is the captain's call (CHECKPOINT-V2.md §"Open"), and the tool reports `STATE-ONLY` for any closure change, per the as-built behaviour.

## What it does not cover (and why)

- **It is not a runner.** A new record's `digest` should be written by the canonical runner, not by this CLI. The CLI is for *validating* the substrate, not for *producing* records. (See `runner/pp.py` in `git.pp-ng`.)
- **It is not a Worker.** The collection pipeline (duplicate-id resolution, divergence warnings) is a Worker concern. This CLI trusts the input it is given.
- **It is not a meta records processor.** Retraction (`dispute`/`settlement` per PR #16) is not yet folded here; the mechanics are in the spike and will land in the referee before they land in this CLI.
- **It is not the most-performant fold.** This is a stdlib-only reimplementation for portability. For the production fold, use the hub.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | VERIFIED, or fold matches, or root matches expected |
| 1 | MISMATCH |
| 2 | STATE-ONLY |
| 3 | Malformed input (orphan cycle, missing field, bad JSON) |
| 4 | Record id not found, or closure incomplete |
| 5 | `--expected` required but not given |

## Use cases

- **PR review.** A reviewer fetches the live records for a project, runs `check --at <pinned>` against the pinned root, and posts the verdict. If `STATE-ONLY`, the PR added a record the pin didn't see — by design, but worth a comment.
- **Bout referee.** A new fold port submits a derived state for the conformance corpus; the referee runs `check --expected <pinned>` and gets a verdict in one command.
- **Local debugging.** A developer changes a record's effect and runs `check --at <that record> --expected <last-known-root>` to see whether the change moved the world or just the closure.

## Test

```bash
python3 -c "
import json, subprocess
records = $(curl -s https://gitpp.superinstance.dev/api/records?project=poc-quilt-1 | jq '.records')
import sys
sys.stdin = open('/dev/null')
"  # convenience one-liner; the real test is the live one above
```

For the real test:

```bash
curl -s https://gitpp.superinstance.dev/api/records?project=poc-quilt-1 | jq '.records' > /tmp/poc.json
python3 gitpp_doctor.py root /tmp/poc.json --at task-c
# must print: 60effe23970d8a5cc837636c44685502361288c1f00577e9addfa10cbd4fc91d
```

## Licence

The same as the hub: per the project's "every README claim is literally true" doctrine, this is a single-file validator with one command and one test. If a use case isn't covered above, the tool's correct answer is "not covered" — the canonical reference is the hub's referee.
