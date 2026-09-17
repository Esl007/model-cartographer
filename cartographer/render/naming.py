"""Deterministic surface naming for a `World`, keyed by (world, seed).

Assigns each `Entity` a human first name matching its gender, each `Obj` a
noun phrase ("the blue box"), and each `Location` a room name, all unique
within a world. Uses the same sha256-stream idiom as `world/generate.py`'s
`_stable_int` so naming is a pure function of (world identity, seed).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..world.model import World

# Pools sized comfortably above any world's entity/location count so retries
# on collision always terminate.
_FIRST_NAMES_F = [
    "Elena", "Priya", "Sofia", "Noor", "Ingrid", "Tamsin", "Wren", "Aria",
    "Beatrix", "Clara", "Dana", "Freya", "Greta", "Hana", "Iris", "Juno",
    "Kira", "Lena", "Maya", "Nadia", "Opal", "Petra", "Quinn", "Rosa",
    "Sana", "Talia", "Uma", "Vera", "Willa", "Xiomara",
]
_FIRST_NAMES_M = [
    "Marcus", "David", "Jamal", "Oscar", "Felix", "Hugo", "Leon", "Rafi",
    "Adrian", "Bruno", "Cyrus", "Dexter", "Elliot", "Finn", "Gideon",
    "Hakim", "Ivo", "Jasper", "Kian", "Lucas", "Milo", "Nolan", "Omar",
    "Piers", "Quentin", "Roman", "Silas", "Tobias", "Victor", "Wyatt",
]
_ROOM_NAMES = [
    "the storage room", "the break room", "the front office",
    "the conference room", "the supply closet", "the kitchen",
    "the lobby", "the workshop", "the archive room", "the server room",
    "the reception area", "the loading dock", "the mail room",
    "the library", "the basement", "the rooftop terrace",
    "the east wing", "the west wing", "the courtyard", "the garage",
]
_OBJECT_ADJECTIVES = [
    "blue", "red", "green", "old", "small", "large", "worn", "shiny",
    "heavy", "narrow", "faded", "dusty", "bright", "cracked", "sturdy",
]
_CATEGORY_NOUNS = {
    "container": "box",
    "document": "folder",
    "tool": "wrench",
    "food": "basket",
    "device": "tablet",
    "decoy": "item",
}


def _stable_int(*parts: str, modulus: int) -> int:
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    as_int = int(digest[:15], 16)
    return as_int % modulus


def _assign_unique(ids: list[str], pool: list[str], seed: int, tag: str, extra_key=lambda i: "") -> dict[str, str]:
    """Assign each id a unique name from pool, retrying on collision."""
    used: set[str] = set()
    result: dict[str, str] = {}
    for entity_id in ids:
        attempt = 0
        while True:
            idx = _stable_int(tag, str(seed), entity_id, extra_key(entity_id), str(attempt), modulus=len(pool))
            candidate = pool[idx]
            if candidate not in used or attempt >= len(pool) * 4:
                used.add(candidate)
                result[entity_id] = candidate
                break
            attempt += 1
    return result


def _normalize(name: str) -> str:
    return name.strip().lower()


@dataclass(frozen=True)
class NameMap:
    entity: dict[str, str] = field(default_factory=dict)
    obj: dict[str, str] = field(default_factory=dict)
    location: dict[str, str] = field(default_factory=dict)

    def location_by_name(self, normalized_name: str) -> str | None:
        target = _normalize(normalized_name)
        for loc_id, name in self.location.items():
            if _normalize(name) == target:
                return loc_id
        return None


def build_name_map(world: World, seed: int) -> NameMap:
    entity_names: dict[str, str] = {}
    used_first_names: set[str] = set()
    for e in world.entities:
        pool = _FIRST_NAMES_F if e.gender == "f" else _FIRST_NAMES_M
        attempt = 0
        while True:
            idx = _stable_int("name_entity", str(seed), e.id, str(attempt), modulus=len(pool))
            candidate = pool[idx]
            if candidate not in used_first_names or attempt >= len(pool) * 4:
                used_first_names.add(candidate)
                entity_names[e.id] = candidate
                break
            attempt += 1

    obj_names: dict[str, str] = {}
    used_obj_names: set[str] = set()
    for o in world.objects:
        noun = _CATEGORY_NOUNS.get(o.category, "item")
        attempt = 0
        while True:
            idx = _stable_int("name_obj_adj", str(seed), o.id, str(attempt), modulus=len(_OBJECT_ADJECTIVES))
            adj = _OBJECT_ADJECTIVES[idx]
            candidate = f"the {adj} {noun}"
            if candidate not in used_obj_names or attempt >= len(_OBJECT_ADJECTIVES) * 4:
                used_obj_names.add(candidate)
                obj_names[o.id] = candidate
                break
            attempt += 1

    location_names: dict[str, str] = {}
    used_room_names: set[str] = set()
    for l in world.locations:
        attempt = 0
        while True:
            idx = _stable_int("name_location", str(seed), l.id, str(attempt), modulus=len(_ROOM_NAMES))
            candidate = _ROOM_NAMES[idx]
            if candidate not in used_room_names or attempt >= len(_ROOM_NAMES) * 4:
                used_room_names.add(candidate)
                location_names[l.id] = candidate
                break
            attempt += 1

    return NameMap(entity=entity_names, obj=obj_names, location=location_names)
