"""Offline deadlock analysis for a two-party message protocol.

Each party (A and B) owns a finite state set. Two queues exist, one per
direction, each with capacity 1:

* ``send``  enqueues a message on the sender's own-direction queue and is
  enabled only while that queue is empty.
* ``receive`` dequeues the message at the head of the opposite-direction
  queue and is enabled only when it matches the transition's message.

Transitions may be interleaved arbitrarily. States from which no transition
is executable are deadlocks unless both parties are simultaneously in final
states with both queues empty (a completed state, which is never expanded).

The analyzer explores the whole reachable state space with breadth-first
search. When several shortest witnesses exist, the one whose transition-id
sequence is lexicographically smallest is returned (enabled successors are
expanded in id order).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

SEND = "send"
RECEIVE = "receive"
PARTIES = ("A", "B")
OTHER = {"A": "B", "B": "A"}

MAX_STATES_PER_PARTY = 8
MAX_TRANSITIONS = 30
MAX_MESSAGES = 8
MAX_NAME_LEN = 64


class ValidationError(Exception):
    """Raised when the protocol description is semantically invalid (HTTP 422)."""


@dataclass(frozen=True)
class Transition:
    id: str
    owner: str  # "A" or "B"
    kind: str  # "send" or "receive"
    source: str
    target: str
    message: str
    peer: str


def _validate_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    if len(value) > MAX_NAME_LEN:
        raise ValidationError(f"{field} must be at most {MAX_NAME_LEN} characters")
    return value


def _check_state_reference(
    state: Any, states: frozenset[str], party: str, field: str
) -> str:
    if not isinstance(state, str) or state not in states:
        raise ValidationError(
            f"{field} '{state}' does not refer to a state of party {party}"
        )
    return state


def _as_string_list(value: Any, field: str, limit: int) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{field} must be a non-empty array")
    if len(value) > limit:
        raise ValidationError(f"{field} must contain at most {limit} items")
    result: list[str] = []
    for index, item in enumerate(value):
        name = _validate_non_empty_str(item, f"{field}[{index}]")
        result.append(name)
    if len(set(result)) != len(result):
        raise ValidationError(f"{field} must not contain duplicate states")
    return result


def _as_transitions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValidationError("transitions must be a non-empty array")
    if len(value) > MAX_TRANSITIONS:
        raise ValidationError(f"transitions must contain at most {MAX_TRANSITIONS} items")
    cleaned: list[dict[str, str]] = []
    for index, item in enumerate(value):
        where = f"transitions[{index}]"
        if not isinstance(item, dict):
            raise ValidationError(f"{where} must be an object")
        for field in ("id", "from", "to", "message"):
            if field not in item:
                raise ValidationError(f"{where} is missing field '{field}'")
        tid = _validate_non_empty_str(item["id"], f"{where}.id")
        message = _validate_non_empty_str(item["message"], f"{where}.message")
        source = _validate_non_empty_str(item["from"], f"{where}.from")
        target = _validate_non_empty_str(item["to"], f"{where}.to")
        kind = item.get("type")
        if kind not in (SEND, RECEIVE):
            raise ValidationError(
                f"{where}.type must be either '{SEND}' or '{RECEIVE}'"
            )
        cleaned.append(
            {
                "id": tid,
                "type": kind,
                "from": source,
                "to": target,
                "message": message,
            }
        )
    ids = [t["id"] for t in cleaned]
    if len(set(ids)) != len(ids):
        raise ValidationError("transition ids must be globally unique")
    return cleaned


def validate_and_build(protocol: dict[str, Any]) -> dict[str, Any]:
    """Validate the raw protocol payload and compile it into lookup tables."""

    if not isinstance(protocol, dict):
        raise ValidationError("request body must be an object")

    states: dict[str, list[str]] = {}
    state_sets: dict[str, frozenset[str]] = {}
    initial: dict[str, str] = {}
    finals: dict[str, frozenset[str]] = {}

    for party in PARTIES:
        spec = protocol.get(party)
        if not isinstance(spec, dict):
            raise ValidationError(f"party {party} must be an object")

        states[party] = _as_string_list(
            spec.get("states"), f"{party}.states", MAX_STATES_PER_PARTY
        )
        state_sets[party] = frozenset(states[party])

        initial[party] = _check_state_reference(
            spec.get("initial"), state_sets[party], party, f"{party}.initial"
        )

        final_list = _as_string_list(
            spec.get("finals"), f"{party}.finals", MAX_STATES_PER_PARTY
        )
        for final_state in final_list:
            _check_state_reference(
                final_state, state_sets[party], party, f"{party}.finals"
            )
        finals[party] = frozenset(final_list)

    raw_transitions = _as_transitions(protocol.get("transitions"))

    transitions: list[Transition] = []
    messages: set[str] = set()
    for raw in raw_transitions:
        source = raw["from"]
        owner = next(
            (p for p in PARTIES if source in state_sets[p]),
            None,
        )
        if owner is None:
            raise ValidationError(
                f"transition '{raw['id']}' source state '{source}' "
                "does not refer to any party state"
            )
        # A state name cannot belong to both parties: overlap would make the
        # owner ambiguous.
        if source in state_sets[OTHER[owner]]:
            raise ValidationError(
                f"transition '{raw['id']}' source state '{source}' "
                "is defined for both parties"
            )
        if raw["to"] not in state_sets[owner]:
            raise ValidationError(
                f"transition '{raw['id']}' target state '{raw['to']}' "
                f"does not refer to a state of party {owner}"
            )
        messages.add(raw["message"])
        transitions.append(
            Transition(
                id=raw["id"],
                owner=owner,
                kind=raw["type"],
                source=source,
                target=raw["to"],
                message=raw["message"],
                peer=OTHER[owner],
            )
        )

    if len(messages) > MAX_MESSAGES:
        raise ValidationError(
            f"at most {MAX_MESSAGES} distinct messages are allowed, "
            f"found {len(messages)}"
        )

    # Outgoing transitions per (party, state), kept sorted by id so that
    # breadth-first expansion follows lexicographical id order.
    outgoing: dict[tuple[str, str], list[Transition]] = {}
    for tr in transitions:
        outgoing.setdefault((tr.owner, tr.source), []).append(tr)
    for trs in outgoing.values():
        trs.sort(key=lambda t: t.id)

    by_id = {t.id: t for t in transitions}

    return {
        "states": states,
        "state_sets": state_sets,
        "initial": initial,
        "finals": finals,
        "transitions": transitions,
        "by_id": by_id,
        "outgoing": outgoing,
    }


def _is_complete(model: dict[str, Any], state: tuple[str, str, str, str]) -> bool:
    a, b, q_ab, q_ba = state
    return (
        a in model["finals"]["A"]
        and b in model["finals"]["B"]
        and q_ab == ""
        and q_ba == ""
    )


def _enabled_successors(
    model: dict[str, Any], state: tuple[str, str, str, str]
) -> list[tuple[Transition, tuple[str, str, str, str]]]:
    a, b, q_ab, q_ba = state
    result: list[tuple[Transition, tuple[str, str, str, str]]] = []

    for party, local_state, own_q, other_q in (
        ("A", a, q_ab, q_ba),
        ("B", b, q_ba, q_ab),
    ):
        for tr in model["outgoing"].get((party, local_state), ()):
            if tr.kind == SEND:
                if own_q == "":
                    if party == "A":
                        nxt = (tr.target, b, tr.message, q_ba)
                    else:
                        nxt = (a, tr.target, q_ab, tr.message)
                else:
                    continue
            else:  # RECEIVE
                if other_q != "" and other_q == tr.message:
                    if party == "A":
                        nxt = (tr.target, b, q_ab, "")
                    else:
                        nxt = (a, tr.target, "", q_ba)
                else:
                    continue
            result.append((tr, nxt))

    # A and B outgoing lists are each id-sorted; merge by id for deterministic
    # lexicographical exploration.
    result.sort(key=lambda pair: pair[0].id)
    return result


def _snapshot(
    model: dict[str, Any], state: tuple[str, str, str, str]
) -> dict[str, Any]:
    a, b, q_ab, q_ba = state
    return {
        "states": {"A": a, "B": b},
        "queues": {
            "A_to_B": [q_ab] if q_ab else [],
            "B_to_A": [q_ba] if q_ba else [],
        },
    }


def analyze(model: dict[str, Any]) -> dict[str, Any]:
    """Run the exhaustive reachability search on a compiled model."""

    start = (
        model["initial"]["A"],
        model["initial"]["B"],
        "",
        "",
    )

    queue: deque[tuple[str, str, str, str]] = deque([start])
    visited: set[tuple[str, str, str, str]] = {start}
    # parent[state] = (previous_state, transition_id)
    parent: dict[tuple[str, str, str, str], tuple[tuple[str, str, str, str], str]] = {}

    first_deadlock: Optional[tuple[str, str, str, str]] = None

    while queue:
        state = queue.popleft()

        if _is_complete(model, state):
            # Completed configuration: successful end, never expanded.
            continue

        successors = _enabled_successors(model, state)
        if not successors:
            if first_deadlock is None:
                first_deadlock = state
            continue

        for tr, nxt in successors:
            if nxt not in visited:
                visited.add(nxt)
                parent[nxt] = (state, tr.id)
                queue.append(nxt)

    result: dict[str, Any] = {
        "deadlock": first_deadlock is not None,
        "visited_states": len(visited),
        "witness": None,
    }

    if first_deadlock is not None:
        # Walk parent pointers back to the initial state.
        chain_ids: list[str] = []
        cursor = first_deadlock
        while cursor != start:
            prev, tid = parent[cursor]
            chain_ids.append(tid)
            cursor = prev
        chain_ids.reverse()

        steps: list[dict[str, Any]] = []
        current = start
        for tid in chain_ids:
            tr = model["by_id"][tid]
            for candidate_tr, nxt in _enabled_successors(model, current):
                if candidate_tr.id == tid:
                    steps.append(
                        {
                            "transition_id": tid,
                            "type": tr.kind,
                            "from": tr.source,
                            "to": tr.target,
                            "message": tr.message,
                            "after": _snapshot(model, nxt),
                        }
                    )
                    current = nxt
                    break

        result["witness"] = {
            "length": len(chain_ids),
            "transition_ids": chain_ids,
            "initial": _snapshot(model, start),
            "steps": steps,
            "deadlock_state": _snapshot(model, first_deadlock),
        }

    return result


def analyze_protocol(protocol: dict[str, Any]) -> dict[str, Any]:
    """Validate and analyze a raw protocol description in one call."""

    return analyze(validate_and_build(protocol))


def apply_transition(
    model: dict[str, Any],
    state: tuple[str, str, str, str],
    transition_id: str,
) -> tuple[str, str, str, str]:
    """Apply one enabled transition; used by tests and callers replaying paths."""

    for tr, nxt in _enabled_successors(model, state):
        if tr.id == transition_id:
            return nxt
    raise ValueError(f"transition '{transition_id}' is not enabled")
