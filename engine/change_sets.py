"""Explicit domain change sets for multi-table engine transitions.

Emitters remain the transaction owners, but they no longer have to discover a
transition while mutating its consequences. These immutable plans separate the
"what changed" decision from SQL application and make the postconditions
inspectable before the baseline advances.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class ChangeSetInvariantError(RuntimeError):
    """A change set was applied without producing its required domain truth."""


@dataclass(frozen=True)
class RosterJoin:
    player_tag: str
    info: Mapping[str, Any]
    dedup_key: str


@dataclass(frozen=True)
class RosterLeave:
    player_tag: str
    info: Mapping[str, Any]
    dedup_key: str


@dataclass(frozen=True)
class RoleTransition:
    player_tag: str
    previous_role: str
    new_role: str
    direction: str
    dedup_key: str


@dataclass(frozen=True)
class RosterChangeSet:
    observed_at: str
    joins: tuple[RosterJoin, ...]
    leaves: tuple[RosterLeave, ...]
    role_transitions: tuple[RoleTransition, ...]


def derive_roster_change_set(
    old_members: Mapping[str, Mapping[str, Any]],
    new_members: Mapping[str, Mapping[str, Any]],
    observed_at: str,
    *,
    role_rank: Mapping[str, int],
) -> RosterChangeSet:
    """Pure roster diff: no database reads, writes, clock calls, or I/O."""
    joins = tuple(
        RosterJoin(
            player_tag=tag,
            info=dict(new_members.get(tag) or {}),
            dedup_key=f"member_joined:{tag}:{observed_at}",
        )
        for tag in sorted(set(new_members) - set(old_members))
    )
    leaves = tuple(
        RosterLeave(
            player_tag=tag,
            info=dict(old_members.get(tag) or {}),
            dedup_key=f"member_left:{tag}:{observed_at}",
        )
        for tag in sorted(set(old_members) - set(new_members))
    )
    roles: list[RoleTransition] = []
    for tag in sorted(set(old_members) & set(new_members)):
        previous = str((old_members.get(tag) or {}).get("role") or "")
        current = str((new_members.get(tag) or {}).get("role") or "")
        if not previous or not current or previous == current:
            continue
        direction = (
            "promoted"
            if role_rank.get(current, -1) > role_rank.get(previous, -1)
            else "demoted"
        )
        roles.append(
            RoleTransition(
                player_tag=tag,
                previous_role=previous,
                new_role=current,
                direction=direction,
                dedup_key=f"role_changed:{tag}:{current}:{observed_at}",
            )
        )
    return RosterChangeSet(observed_at, joins, leaves, tuple(roles))


@dataclass(frozen=True)
class SeasonCloseChangeSet:
    season_id: int
    observed_at: str
    final_rank: int | None
    weeks: int
    war_champ_tag: str | None
    free_pass_tag: str | None
    outcome: Mapping[str, Any]
    event_payload: Mapping[str, Any]
    event_dedup_key: str


__all__ = [
    "ChangeSetInvariantError",
    "RosterChangeSet",
    "RosterJoin",
    "RosterLeave",
    "RoleTransition",
    "SeasonCloseChangeSet",
    "derive_roster_change_set",
]
