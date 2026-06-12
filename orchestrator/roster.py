"""Roster: active teams, role catalog, and aliases from config/roster.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class SubTeam:
    id: str
    name: str
    function: str       # dev | qa
    issue_prefix: str


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    role: str
    alias: str
    issue_prefix: str
    repos: tuple[str, ...] = ()   # repos this team works in; () = unmapped
    sub_teams: tuple[SubTeam, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Roster:
    teams: dict[str, Team]
    aliases: dict[str, str]
    enabled_skills: tuple[str, ...]
    default_runtime: str
    roles: dict[str, Any]

    def resolve(self, name: str) -> Optional[Team]:
        """Resolve a team id or alias to a Team."""
        key = self.aliases.get(name, name)
        return self.teams.get(key)


def load_roster(config: dict[str, Any]) -> Roster:
    teams: dict[str, Team] = {}
    for spec in config.get("active_teams", []) or []:
        subs = tuple(
            SubTeam(
                id=s["id"],
                name=s.get("name", s["id"]),
                function=s.get("function", "dev"),
                issue_prefix=s.get("issue_prefix", ""),
            )
            for s in spec.get("sub_teams", []) or []
        )
        teams[spec["id"]] = Team(
            id=spec["id"],
            name=spec.get("name", spec["id"]),
            role=spec.get("role", spec["id"]),
            alias=spec.get("alias", spec["id"]),
            issue_prefix=spec.get("issue_prefix", ""),
            # registry.yaml spec: each team names its repo(s); accept either key
            repos=tuple(spec.get("repos") or
                        ([spec["repo"]] if spec.get("repo") else [])),
            sub_teams=subs,
        )
    return Roster(
        teams=teams,
        aliases=dict(config.get("aliases", {}) or {}),
        enabled_skills=tuple(config.get("enabled_skills", []) or []),
        default_runtime=config.get("default_runtime", "api"),
        roles=config.get("roles", {}) or {},
    )
