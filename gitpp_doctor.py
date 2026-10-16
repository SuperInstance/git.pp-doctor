#!/usr/bin/env python3
"""
gitpp-doctor — validate a git.pp record set against the pinned closure root.

A record-set validator and fold replayer, byte-identical to the canonical
implementation in `patchwork-quilt/git.pp-ng/referee/fold.py` for the
operations it supports. The intent is *not* to be the fold — the fold lives
in the hub and its siblings. The intent is a single, no-setup, stdlib-only
command that asks three questions of a record set:

  1. Is this set well-formed? (no orphan cycles, no malformed records)
  2. What is the closure root at <at>? (SHA-256 over the pinned leaf domain)
  3. Does it match the pinned root? (VERIFIED / STATE-ONLY / MISMATCH)
  4. (Optional) What is the derived state? (the fold, replayed)

The closure-root and leaf construction follow docs/CHECKPOINT-V2.md in
patchwork-quilt/git.pp-ng. The fold follows DESIGN.md. Both are
re-implemented in stdlib only so a `pip install` is never required.

Usage:
    gitpp_doctor.py check <file> --at <record> [--expected <root>]
    gitpp_doctor.py fold <file>
    gitpp_doctor.py root <file> --at <record>
    gitpp_doctor.py --version

Exit codes:
    0  VERIFIED, or fold matches expected state, or root matches expected
    1  MISMATCH (state differs, or root differs)
    2  STATE-ONLY (closure changed since the pinned root)
    3  malformed input (orphan cycle, missing field, bad JSON)
    4  record not found
    5  --expected missing where required
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any, Dict, List, Optional, Tuple


# ---------- leaf construction (format v2, CHECKPOINT-V2.md) ----------

def _float64_canonical(v: float) -> float:
    """Pin a float to its IEEE-754 double representation. JS `String(v)` and
    Python `repr(v)` diverge for integral doubles in [1e16, 1e21); canonical
    values here before the encoding step."""
    # ±0.0 -> 0
    if v == 0.0:
        return 0.0
    return float(v)


def _leaf_number(v: Any) -> Any:
    """Render one number to the canonical form both ports compute identically.
    See spec §3 'leaf-number encoding'.

    Integer form requires |v| < 1e16 to fit safely in int64 across all 5
    ports. Values that would overflow int64 (Python's arbitrary-precision
    ints would otherwise emit 21-digit numbers C# and C cannot produce)
    fall back to scientific notation, matching what the other 4 ports emit.
    """
    if isinstance(v, bool):
        return v  # bool is a kind of int in Python; preserve as bool
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        v = _float64_canonical(v)
        if v.is_integer() and abs(v) < 1e16:
            return int(v)
        # Non-integral or |v| >= 1e16: rely on Python's repr (shortest
        # round-trip), which is what json.dumps will use anyway.
        return v
    return v


def _canonicalize(record: Dict[str, Any]) -> Dict[str, Any]:
    """Build the leaf-normalized form of a record (CHECKPOINT-V2.md §'Record leaf')."""
    parents = list(record.get("parents", []) or [])
    # parents is a set: sort by Unicode code point
    parents_sorted = sorted(parents, key=lambda s: [ord(c) for c in str(s)])
    effects_raw = record.get("effects", {}) or {}
    # effects: same shape as input, but numbers leaf-number-encoded
    effects_canon: Dict[str, Any] = {}
    for k in sorted(effects_raw.keys()):
        v = effects_raw[k]
        if isinstance(v, dict):
            # nested effects: recurse over the dict
            inner = {}
            for ik in sorted(v.keys()):
                inner[ik] = _leaf_number(v[ik])
            effects_canon[k] = inner
        elif isinstance(v, list):
            effects_canon[k] = [_leaf_number(x) for x in v]
        else:
            effects_canon[k] = _leaf_number(v)
    return {
        "id": str(record.get("id", "")),
        "parents": [str(p) for p in parents_sorted],
        "kind": record.get("kind"),
        "effects": effects_canon,
        "reason": record.get("reason"),
    }


def record_leaf(record: Dict[str, Any]) -> str:
    """SHA-256 hex of the record's leaf, lowercase."""
    canon = _canonicalize(record)
    # Python's json.dumps with sort_keys, ensure_ascii, the canonical
    # separators is what the hub pins:
    #   json.dumps(x, sort_keys=True, ensure_ascii=False,
    #              separators=(", ", ": "))
    payload = json.dumps(
        canon,
        sort_keys=True,
        ensure_ascii=False,
        separators=(", ", ": "),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def closure_of(records: List[Dict[str, Any]], at: str) -> Optional[List[str]]:
    """The set of record ids in `at`'s transitive ancestor closure, plus `at`
    itself, intersected with the records present. Returns None if any
    ancestor is missing (incomplete closure, cannot root)."""
    by_id = {r.get("id"): r for r in records if r.get("id") is not None}
    if at not in by_id:
        return None
    seen: List[str] = []
    visited = set()
    stack = [at]
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue  # already processed: skip
        if cur not in by_id:
            return None  # missing ancestor
        visited.add(cur)
        seen.append(cur)
        for p in by_id[cur].get("parents", []) or []:
            if p not in visited:
                stack.append(p)
    return sorted(seen, key=lambda s: [ord(c) for c in s])


def closure_root(records: List[Dict[str, Any]], at: str) -> Optional[str]:
    """SHA-256 of '\\n'.join('id:leaf') over the closure of `at`, sorted by
    Unicode code point. None if closure is incomplete."""
    closure = closure_of(records, at)
    if closure is None:
        return None
    by_id = {r.get("id"): r for r in records if r.get("id") is not None}
    payload = "\n".join(f"{rid}:{record_leaf(by_id[rid])}" for rid in closure)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------- fold (DESIGN.md) ----------
# Pure-Python reimplementation of the fold's resolution algorithm.
# Matches the TS port on every named case in the conformance corpus.
# For the purposes of git.pp-doctor, the fold is *advisory*: this CLI is
# for validating the substrate, not for running the production system.

def derive(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return {state, order, conflicts, orphans} for a record set, following
    DESIGN.md's resolution rules. Float64 numbers, unicode-codepoint id
    ordering, Kahn-with-smallest-id-first, class-precedence override>task>witness,
    ancestry reduction, cleanest-chain tie-break, conflict losers preserved."""
    by_id: Dict[str, Dict[str, Any]] = {r["id"]: r for r in records if r.get("id")}
    # 1. Canonical indexing
    # 2. Orphan worklist
    referenced = set()
    for r in records:
        for p in r.get("parents", []) or []:
            referenced.add(p)
    # An orphan is a record that is itself in the set, or that references
    # a record not in the set. (DESIGN: orphan = any missing parent in
    # transitive closure.)
    missing = set()
    for r in records:
        for p in r.get("parents", []) or []:
            if p not in by_id:
                missing.add(p)
    # Exclude cycle-affected records: detect cycles, drop all involved.
    # (Cycle detection via DFS coloring.)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {rid: WHITE for rid in by_id}
    cycle = set()
    def dfs(rid: str, path: List[str]) -> None:
        c = color.get(rid, WHITE)
        if c == GRAY:
            # cycle: mark everyone in path
            idx = path.index(rid)
            for x in path[idx:]:
                cycle.add(x)
            return
        if c == BLACK:
            return
        color[rid] = GRAY
        path.append(rid)
        for p in by_id[rid].get("parents", []) or []:
            if p in by_id:
                dfs(p, path)
        path.pop()
        color[rid] = BLACK
    for rid in list(by_id.keys()):
        if color[rid] == WHITE:
            dfs(rid, [])
    orphans = {rid for rid in by_id if any(p not in by_id for p in by_id[rid].get("parents", []) or [])} | cycle
    applied = {rid: r for rid, r in by_id.items() if rid not in orphans}
    # 3. Kahn order, smallest-id-first
    in_deg: Dict[str, int] = {rid: 0 for rid in applied}
    children: Dict[str, List[str]] = {rid: [] for rid in applied}
    for rid, r in applied.items():
        for p in r.get("parents", []) or []:
            if p in applied:
                in_deg[rid] += 1
                children[p].append(rid)
    ready = sorted([rid for rid, d in in_deg.items() if d == 0], key=lambda s: [ord(c) for c in s])
    order: List[str] = []
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        for c in children[cur]:
            in_deg[c] -= 1
            if in_deg[c] == 0:
                ready.append(c)
        ready.sort(key=lambda s: [ord(c) for c in s])
    # 4. Per-key resolution
    # For each key K, gather all applied records whose effects set K.
    # Among them, by absolute class precedence: override > task > witness.
    # Within that class, ancestry-reduce (most-derived survive).
    # Tie-break: most intent ancestors, fewest witness ancestors, smallest id.
    CLASS = {"override": 3, "task": 2, "witness": 1}
    state: Dict[str, Any] = {}
    conflicts: List[Dict[str, Any]] = []
    # All keys touched
    keys: set = set()
    for r in applied.values():
        keys.update((r.get("effects") or {}).keys())
    for K in keys:
        candidates = [r for r in applied.values() if K in (r.get("effects") or {})]
        if not candidates:
            continue
        # Class filter
        top_class = max(CLASS.get(r.get("kind"), 0) for r in candidates)
        top = [r for r in candidates if CLASS.get(r.get("kind"), 0) == top_class]
        # Ancestry reduction: a candidate is dominated if any other candidate
        # is its ancestor (transitive).
        def ancestors_of(rid: str) -> set:
            seen = set()
            stack = [rid]
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                for p in applied[x].get("parents", []) or []:
                    if p in applied:
                        stack.append(p)
            return seen
        anc = {r["id"]: ancestors_of(r["id"]) for r in top}
        survivors = []
        for r in top:
            # Per spec §5.2: a candidate is DOMINATED if it is a (transitive)
            # ANCESTOR of another candidate at the same class. Survivors are
            # the most-derived. (This is the inverse of the wrong test
            # "other is an ancestor of r", which selected the OLDEST record
            # in the chain instead of the newest — see gitpp-doctor v0.2.2.)
            dominated = any(r["id"] in anc[other["id"]] for other in top if other["id"] != r["id"])
            if not dominated:
                survivors.append(r)
        # Tie-break
        def intent_count(rid: str) -> int:
            return sum(1 for x in anc[rid] if applied[x].get("kind") in ("task", "override"))
        def witness_count(rid: str) -> int:
            return sum(1 for x in anc[rid] if applied[x].get("kind") == "witness")
        if len(survivors) > 1:
            survivors.sort(key=lambda r: (
                -intent_count(r["id"]),
                witness_count(r["id"]),
                [ord(c) for c in r["id"]],
            ))
        # Per DESIGN.md §2 step 5: "If, after resolution, there are two or
        # more distinct values for a key, a conflict is emitted. The losers
        # are all candidates except the winner." This holds even when class
        # precedence alone decides the winner: a witness in a contested
        # key is named a loser, never silently dropped.
        winner = survivors[0]
        winner_value = winner["effects"][K]
        state[K] = winner_value
        distinct_values = {r["effects"][K] for r in candidates}
        if len(distinct_values) > 1:
            losers = sorted(
                [r["id"] for r in candidates if r["id"] != winner["id"] and r["effects"][K] != winner_value],
                key=lambda s: [ord(c) for c in s],
            )
            if losers:
                conflicts.append({"key": K, "winner": winner["id"], "losers": losers})
    return {
        "state": state,
        "order": order,
        "conflicts": conflicts,
        "orphans": sorted(orphans, key=lambda s: [ord(c) for c in s]),
    }


# ---------- doctor commands ----------

def cmd_compare(args: argparse.Namespace) -> int:
    """Compare two record sets: closure roots at <at>, derived states, deltas.

    Useful for: "what changed between the last pinned checkpoint and the
    current set?" Reports a structured diff, not a yes/no answer.
    """
    rec_a = _load_records_arg(args.file_a)
    rec_b = _load_records_arg(args.file_b)

    # Closure roots
    root_a = closure_root(rec_a, args.at)
    root_b = closure_root(rec_b, args.at)

    # Derived states
    state_a = derive(rec_a)["state"]
    state_b = derive(rec_b)["state"]

    # Order
    order_a = [r for r in derive(rec_a)["order"]]
    order_b = [r for r in derive(rec_b)["order"]]

    # Conflicts
    conflicts_a = derive(rec_a)["conflicts"]
    conflicts_b = derive(rec_b)["conflicts"]

    # State diff
    only_a = {k: state_a[k] for k in state_a if k not in state_b or state_a[k] != state_b.get(k)}
    only_b = {k: state_b[k] for k in state_b if k not in state_a or state_b[k] != state_a.get(k)}

    # Order diff
    only_order_a = [r for r in order_a if r not in order_b]
    only_order_b = [r for r in order_b if r not in order_a]

    # Conflict diff
    only_conflicts_a = [c for c in conflicts_a if c not in conflicts_b]
    only_conflicts_b = [c for c in conflicts_b if c not in conflicts_a]

    out = {
        "at": args.at,
        "a": args.file_a,
        "b": args.file_b,
        "root": {
            "a": root_a,
            "b": root_b,
            "match": root_a == root_b,
        },
        "state": {
            "onlyInA": only_a,
            "onlyInB": only_b,
            "match": only_a == {} and only_b == {},
        },
        "order": {
            "onlyInA": only_order_a,
            "onlyInB": only_order_b,
            "match": only_order_a == [] and only_order_b == [],
        },
        "conflicts": {
            "onlyInA": only_conflicts_a,
            "onlyInB": only_conflicts_b,
            "match": only_conflicts_a == [] and only_conflicts_b == [],
        },
    }
    # If the user provided --expected, also report that
    if args.expected:
        out["expected"] = args.expected
        out["expectedMatchA"] = (root_a == args.expected)
        out["expectedMatchB"] = (root_b == args.expected)
    print(json.dumps(out, indent=2))
    # Exit code: 0 if everything matches, 1 if anything differs
    if (out["root"]["match"] and out["state"]["match"]
            and out["order"]["match"] and out["conflicts"]["match"]):
        return 0
    return 1


def cmd_explain(args: argparse.Namespace) -> int:
    """Explain a record set: closure, leaves, state, conflicts, root.

    Useful for: "what does this record set derive to, and why?"
    Shows the closure in topo order, each record's leaf hash, the
    derived state with each key's setter, the conflicts, and the
    closure root.
    """
    records = _load_records(args)
    at = args.at
    root = closure_root(records, at)
    derived = derive(records)

    # Compute the closure (topo order)
    by_id = {r["id"]: r for r in records if r.get("id")}
    if at not in by_id:
        print(json.dumps({"ok": False, "reason": f"record '{at}' not found in set"}, indent=2))
        return 4
    seen = set()
    stack = [at]
    closure = []
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        if cur not in by_id:
            print(json.dumps({"ok": False, "reason": f"closure incomplete: parent '{cur}' missing", "at": at}, indent=2))
            return 4
        seen.add(cur)
        closure.append(cur)
        for p in by_id[cur].get("parents", []) or []:
            if p not in seen:
                stack.append(p)

    # Build per-record leaf and setter info
    by_id_in_closure = {rid: by_id[rid] for rid in closure}
    closure_records = []
    for rid in sorted(closure, key=lambda x: [ord(c) for c in x]):
        r = by_id_in_closure[rid]
        leaf = record_leaf(r)
        setters = [(k, derived["order"].index(s) if s in derived["order"] else -1, s)
                    for k, s in [] if False]  # placeholder; we use derived['state'] lookup below
        # Find which keys this record set in the derived state
        keys_set = []
        for k, v in derived["state"].items():
            # Walk the order: if applying this record would have set the key,
            # and the key's value matches this record's effect, then yes.
            # Simpler: check that this record's effects match a state key.
            for ek, ev in (r.get("effects") or {}).items():
                if ek == k and ev == v:
                    keys_set.append(k)
        closure_records.append({
            "id": rid,
            "kind": r.get("kind"),
            "parents": sorted(r.get("parents", []) or [], key=lambda x: [ord(c) for c in x]),
            "effects": r.get("effects", {}),
            "leaf": leaf,
            "sets": keys_set,
        })

    # Identify the setter for each state key
    setters = {}
    for k in derived["state"]:
        for rid in derived["order"]:
            r = by_id[rid]
            for ek, ev in (r.get("effects") or {}).items():
                if ek == k:
                    setters[k] = rid
                    break
            if k in setters:
                break

    state_with_setters = {k: {"value": v, "setBy": setters.get(k)} for k, v in derived["state"].items()}

    out = {
        "ok": True,
        "at": at,
        "closure": closure,
        "closureRoot": root,
        "closureRecords": closure_records,
        "state": derived["state"],
        "stateWithSetters": state_with_setters,
        "order": derived["order"],
        "conflicts": derived["conflicts"],
        "orphans": sorted(list(derived["orphans"])),
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_root(args: argparse.Namespace) -> int:
    records = _load_records(args)
    root = closure_root(records, args.at)
    if root is None:
        print(json.dumps({"ok": False, "reason": "incomplete-closure-or-missing", "at": args.at}, indent=2))
        return 4
    out = {"ok": True, "at": args.at, "closureRoot": root}
    if args.expected:
        out["expected"] = args.expected
        out["match"] = (root == args.expected)
    print(json.dumps(out, indent=2))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Three-way check: VERIFIED / STATE-ONLY / MISMATCH.

    Without --expected: print the leaf, root, and a fold summary; exit 0.
    With --expected: compare and exit accordingly.
    """
    records = _load_records(args)
    leaf_of = {r["id"]: record_leaf(r) for r in records if r.get("id")}
    root = closure_root(records, args.at)
    if root is None:
        print(json.dumps({"ok": False, "reason": "incomplete-closure-or-missing", "at": args.at}, indent=2))
        return 4
    d = derive(records)
    out: Dict[str, Any] = {
        "ok": True,
        "at": args.at,
        "closureRoot": root,
        "leaves": {rid: leaf_of[rid] for rid in sorted(leaf_of, key=lambda s: [ord(c) for c in s])},
        "applied": sorted([r["id"] for r in records if r["id"] not in d["orphans"]], key=lambda s: [ord(c) for c in s]),
        "orphans": d["orphans"],
        "state": d["state"],
        "conflicts": d["conflicts"],
    }
    if not args.expected:
        print(json.dumps(out, indent=2))
        return 0
    out["expected"] = args.expected
    if root == args.expected:
        out["verdict"] = "VERIFIED"
        print(json.dumps(out, indent=2))
        return 0
    # Closure root differs. Cannot distinguish grown/shrunk/swapped from a
    # single root (CHECKPOINT-V2.md §'Open: shrunk/swapped classification'),
    # so the honest verdict is STATE-ONLY unless the state itself moved.
    # If the caller supplied --expected-state, we can distinguish.
    if args.expected_state:
        try:
            expected_state = json.loads(args.expected_state)
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False, "reason": f"bad --expected-state JSON: {exc}"}, indent=2))
            return 3
        if d["state"] != expected_state:
            out["verdict"] = "MISMATCH"
            out["stateDiff"] = _state_diff(d["state"], expected_state)
            print(json.dumps(out, indent=2))
            return 1
    out["verdict"] = "STATE-ONLY"
    print(json.dumps(out, indent=2))
    return 2


def cmd_fold(args: argparse.Namespace) -> int:
    records = _load_records(args)
    d = derive(records)
    print(json.dumps(d, indent=2))
    return 0


# ---------- input loaders ----------

def _state_diff(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the symmetric diff between two state dicts, used to make
    MISMATCH verdicts diagnosable."""
    only_a = {k: a[k] for k in a if k not in b or a[k] != b.get(k)}
    only_b = {k: b[k] for k in b if k not in a or b[k] != a.get(k)}
    return {"onlyInFolded": only_a, "onlyInExpected": only_b}


def _load_records_arg(filename: str) -> List[Dict[str, Any]]:
    if filename and filename != "-":
        with open(filename) as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)
    if isinstance(data, dict) and "records" in data:
        return data["records"]
    if isinstance(data, list):
        return data
    raise ValueError("input must be a JSON list of records or {'records': [...]}")


def _load_records(args: argparse.Namespace) -> List[Dict[str, Any]]:
    return _load_records_arg(args.file)


# ---------- CLI ----------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="gitpp-doctor", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("root", help="compute the closure root at --at")
    pp.add_argument("file", help="record-set JSON file, or '-' for stdin")
    pp.add_argument("--at", required=True, help="record id to root at")
    pp.add_argument("--expected", help="pinned root to compare against")
    pp.set_defaults(func=cmd_root)
    pex = sub.add_parser("explain", help="explain a record set: closure, leaves, state, conflicts, root")
    pex.add_argument("file")
    pex.add_argument("--at", required=True, help="record id to root at")
    pex.set_defaults(func=cmd_explain)
    pcm = sub.add_parser("compare", help="compare two record sets: roots, states, orders, conflicts")
    pcm.add_argument("file_a", help="first record-set JSON file")
    pcm.add_argument("file_b", help="second record-set JSON file")
    pcm.add_argument("--at", required=True, help="record id to root at (used for the closure)")
    pcm.add_argument("--expected", help="(optional) pinned root to also report match for A and B")
    pcm.set_defaults(func=cmd_compare)
    pc = sub.add_parser("check", help="three-way check (VERIFIED/STATE-ONLY/MISMATCH)")
    pc.add_argument("file")
    pc.add_argument("--at", required=True)
    pc.add_argument("--expected", help="pinned closure root")
    pc.add_argument("--expected-state", help="pinned state JSON; if given, MISMATCH is reported when the fold's state differs from this")
    pc.set_defaults(func=cmd_check)
    pf = sub.add_parser("fold", help="replay the fold and print derived state")
    pf.add_argument("file")
    pf.set_defaults(func=cmd_fold)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
