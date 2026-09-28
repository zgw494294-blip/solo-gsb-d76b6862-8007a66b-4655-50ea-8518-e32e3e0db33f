"""Tests for the reachability/deadlock solver (stdlib only).

Run with: python tests/test_solver.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.solver import ValidationError, analyze_protocol  # noqa: E402


def p(
    a_states,
    b_states,
    transitions,
    a_init=None,
    b_init=None,
    a_finals=None,
    b_finals=None,
):
    return {
        "A": {
            "states": a_states,
            "initial": a_init or a_states[0],
            "finals": a_finals or [a_states[-1]],
        },
        "B": {
            "states": b_states,
            "initial": b_init or b_states[0],
            "finals": b_finals or [b_states[-1]],
        },
        "transitions": transitions,
    }


def t(tid, kind, src, dst, msg):
    return {"id": tid, "type": kind, "from": src, "to": dst, "message": msg}


CASES: list[tuple[str, object]] = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn

    return deco


@case("ping-pong completes without deadlock")
def _():
    protocol = p(
        ["a0", "a1", "a2"],
        ["b0", "b1", "b2"],
        [
            t("t1", "send", "a0", "a1", "ping"),
            t("t2", "receive", "b0", "b1", "ping"),
            t("t3", "send", "b1", "b2", "pong"),
            t("t4", "receive", "a1", "a2", "pong"),
        ],
    )
    r = analyze_protocol(protocol)
    assert r["deadlock"] is False, r
    assert r["witness"] is None
    # start, ping in flight, both middle states, complete state
    assert r["visited_states"] == 5, r


@case("receive mismatch leaves deadlock with message stuck in queue")
def _():
    # A sends x and stops (a1 non-final); B only accepts z, so the slot can
    # never be drained.
    protocol = p(
        ["a0", "a1", "a2"],
        ["b0", "b1"],
        [
            t("s1", "send", "a0", "a1", "x"),
            t("s2", "send", "a1", "a2", "y"),
            t("r1", "receive", "b0", "b1", "z"),
        ],
        b_finals=["b1"],
    )
    r = analyze_protocol(protocol)
    assert r["deadlock"] is True
    w = r["witness"]
    assert w["transition_ids"] == ["s1"]
    assert w["length"] == 1
    assert w["initial"]["queues"] == {"A_to_B": [], "B_to_A": []}
    assert w["steps"][0]["after"]["queues"]["A_to_B"] == ["x"]
    assert w["deadlock_state"]["states"] == {"A": "a1", "B": "b0"}
    assert w["deadlock_state"]["queues"]["A_to_B"] == ["x"]
    assert w["deadlock_state"]["queues"]["B_to_A"] == []
    # s2 blocked (slot occupied), r1 not matching (x != z)
    assert r["visited_states"] == 2, r


@case("deadlock with empty queues: both parties stopped but not both final")
def _():
    protocol = {
        "A": {"states": ["a0", "a1", "adone"], "initial": "a0", "finals": ["adone"]},
        "B": {"states": ["b0", "b1"], "initial": "b0", "finals": ["b1"]},
        "transitions": [
            t("t1", "send", "a0", "a1", "ping"),
            t("t2", "receive", "b0", "b1", "ping"),
        ],
    }
    # (a1, b1, empty): B final, A not final, no outgoing transitions -> deadlock
    r = analyze_protocol(protocol)
    assert r["deadlock"] is True
    assert r["witness"]["transition_ids"] == ["t1", "t2"]
    assert r["witness"]["deadlock_state"]["queues"] == {
        "A_to_B": [],
        "B_to_A": [],
    }
    assert r["visited_states"] == 3, r


@case("initial state is an immediate deadlock")
def _():
    protocol = p(
        ["a0", "a1"],
        ["b0", "b1"],
        [t("t1", "send", "a1", "a0", "m")],
        a_finals=["a1"],
        b_finals=["b1"],
    )
    r = analyze_protocol(protocol)
    assert r["deadlock"] is True
    assert r["witness"]["length"] == 0
    assert r["witness"]["transition_ids"] == []
    assert r["witness"]["steps"] == []
    assert r["witness"]["deadlock_state"]["states"] == {"A": "a0", "B": "b0"}
    assert r["visited_states"] == 1


@case("send blocked when own-direction slot occupied")
def _():
    protocol = p(
        ["a0", "a1", "a2"],
        ["b0", "b1"],
        [
            t("s1", "send", "a0", "a1", "x"),
            t("s2", "send", "a1", "a2", "y"),
            t("r1", "receive", "b0", "b1", "z"),
        ],
        b_finals=["b1"],
    )
    r = analyze_protocol(protocol)
    assert r["deadlock"] is True
    assert r["witness"]["transition_ids"] == ["s1"]
    assert r["visited_states"] == 2


@case("completed state is never expanded even if it has outgoing edges")
def _():
    protocol = p(
        ["a0", "a1", "a2"],
        ["b0", "b1", "b2"],
        [
            t("t1", "send", "a0", "a1", "ping"),
            t("t2", "receive", "b0", "b1", "ping"),
            t("t3", "send", "b1", "b2", "pong"),
            t("t4", "receive", "a1", "a2", "pong"),
            t("t5", "send", "a2", "a0", "again"),
        ],
    )
    r = analyze_protocol(protocol)
    assert r["deadlock"] is False
    assert r["visited_states"] == 5, r  # t5 from the complete state is ignored


@case("lexicographic tie-break among equal-length deadlocks")
def _():
    # From a0 two sends (ids p, q) each reach a stuck state at depth 1, since
    # B has no transition at all; B b0 is not final.
    protocol = p(
        ["a0", "a1", "a2"],
        ["b0", "b1"],
        [
            t("q", "send", "a0", "a2", "m2"),
            t("p", "send", "a0", "a1", "m1"),
        ],
        a_finals=["a1", "a2"],
        b_finals=["b1"],
    )
    # Both targets are A-final but the queue is non-empty and B is not final,
    # so neither configuration is complete and both are deadlocks.
    r = analyze_protocol(protocol)
    assert r["deadlock"] is True
    assert r["witness"]["transition_ids"] == ["p"], r["witness"]
    assert r["visited_states"] == 3


@case("shortest deadlock wins over lexicographically smaller longer path")
def _():
    # "a1x" sorts before "c0z" but deadlocks through it only at depth 3,
    # whereas "c0z" deadlocks immediately at depth 1.
    protocol = {
        "A": {"states": ["a0", "a1"], "initial": "a0", "finals": ["a1"]},
        "B": {"states": ["b0", "b1"], "initial": "b0", "finals": ["b1"]},
        "transitions": [
            t("a1x", "send", "a0", "a0", "x"),
            t("a2r", "receive", "b0", "b1", "x"),
            t("c0z", "send", "a0", "a1", "z"),
        ],
    }
    r = analyze_protocol(protocol)
    assert r["deadlock"] is True, r
    assert r["witness"]["transition_ids"] == ["c0z"], r["witness"]
    assert r["witness"]["deadlock_state"]["queues"]["A_to_B"] == ["z"]
    # start; (a0,b0,x); (a1,b0,z)=deadlock; (a0,b1,empty); the two depth-3
    # stuck states reached via B b1 before exploration stops expanding
    assert r["visited_states"] == 6, r


@case("deadlock reached at depth 2 via forced order")
def _():
    protocol = p(
        ["a0", "a1"],
        ["b0", "b1"],
        [
            t("zloop", "send", "a0", "a0", "q"),
            t("arecv", "receive", "b0", "b1", "q"),
        ],
        a_finals=["a1"],
        b_finals=["b1"],
    )
    r = analyze_protocol(protocol)
    assert r["deadlock"] is True
    # S2=(a0,b1,empty) still has zloop enabled, so the first deadlock is one
    # step deeper, after the second send stalls with B already final.
    assert r["witness"]["transition_ids"] == ["zloop", "arecv", "zloop"]
    assert r["witness"]["deadlock_state"]["states"] == {"A": "a0", "B": "b1"}
    assert r["witness"]["deadlock_state"]["queues"]["A_to_B"] == ["q"]
    assert r["visited_states"] == 4, r


@case("two-direction handshake state count")
def _():
    # Alternating messages with 1-slot queues: every state has at most one
    # enabled move and the run completes.
    protocol = {
        "A": {"states": ["a0", "a1", "a2"], "initial": "a0", "finals": ["a2"]},
        "B": {"states": ["b0", "b1", "b2"], "initial": "b0", "finals": ["b2"]},
        "transitions": [
            t("1", "send", "a0", "a1", "m1"),
            t("2", "receive", "b0", "b1", "m1"),
            t("3", "send", "a1", "a2", "m2"),
            t("4", "receive", "b1", "b2", "m2"),
        ],
    }
    r = analyze_protocol(protocol)
    assert r["deadlock"] is False
    assert r["visited_states"] == 5


# ---------------------------------------------------------------------------
# 422 validation cases
# ---------------------------------------------------------------------------


INVALID_CASES: list[tuple[str, dict, str]] = []


def invalid(name, protocol, fragment):
    INVALID_CASES.append((name, protocol, fragment))


invalid(
    "initial references unknown state",
    p(["a0"], ["b0"], [t("x", "send", "a0", "a0", "m")], a_init="zz"),
    "initial",
)
invalid(
    "final references unknown state",
    p(["a0"], ["b0"], [t("x", "send", "a0", "a0", "m")], b_finals=["zz"]),
    "finals",
)
invalid(
    "transition target references other party",
    p(["a0"], ["b0", "b1"], [t("x", "send", "a0", "b1", "m")]),
    "target state",
)
invalid(
    "transition source unknown",
    p(["a0"], ["b0"], [t("x", "send", "zz", "a0", "m")]),
    "source state",
)
invalid(
    "duplicate transition ids",
    p(
        ["a0", "a1"],
        ["b0"],
        [t("x", "send", "a0", "a1", "m"), t("x", "send", "a0", "a1", "n")],
    ),
    "unique",
)
invalid(
    "duplicate states within a party",
    p(["a0", "a0"], ["b0"], [t("x", "send", "a0", "a0", "m")]),
    "duplicate",
)
invalid(
    "too many states",
    p([f"a{i}" for i in range(9)], ["b0"], [t("x", "send", "a0", "a0", "m")]),
    "at most 8",
)


def _big_protocol(kind):
    if kind == "transitions":
        trs = [
            {"id": f"t{i}", "type": "send", "from": "a0", "to": "a0", "message": "m"}
            for i in range(31)
        ]
        return p(["a0"], ["b0"], trs)
    if kind == "messages":
        trs = [
            {
                "id": f"t{i}",
                "type": "send",
                "from": "a0",
                "to": "a0",
                "message": f"m{i}",
            }
            for i in range(9)
        ]
        return p(["a0"], ["b0"], trs)


invalid("more than 30 transitions", _big_protocol("transitions"), "at most 30")
invalid("more than 8 message kinds", _big_protocol("messages"), "distinct messages")
invalid(
    "bad transition type",
    p(["a0"], ["b0"], [{"id": "x", "type": "wait", "from": "a0", "to": "a0", "message": "m"}]),
    "type",
)
invalid(
    "empty states list",
    {
        "A": {"states": [], "initial": "a0", "finals": ["a0"]},
        "B": {"states": ["b0"], "initial": "b0", "finals": ["b0"]},
        "transitions": [t("x", "send", "a0", "a0", "m")],
    },
    "non-empty",
)


def run():
    failures = 0

    for name, fn in CASES:
        try:
            fn()
        except AssertionError:
            failures += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}")
            traceback.print_exc()
        else:
            print(f"ok    {name}")

    for name, protocol, fragment in INVALID_CASES:
        try:
            analyze_protocol(protocol)
        except ValidationError as exc:
            if fragment.lower() in str(exc).lower():
                print(f"ok    422: {name}")
            else:
                failures += 1
                print(f'FAIL  422: {name}: message "{exc}" lacks "{fragment}"')
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR 422: {name}: {exc!r}")
            traceback.print_exc()
        else:
            failures += 1
            print(f"FAIL  422: {name}: no ValidationError raised")

    total = len(CASES) + len(INVALID_CASES)
    print(f"\n{total - failures}/{total} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
