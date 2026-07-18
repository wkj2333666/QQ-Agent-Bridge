"""Coordinate one bounded artifact repair attempt."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .outgoing_resources import ArtifactInspection, OutgoingResource

InspectArtifacts = Callable[[str], ArtifactInspection]
RepairArtifacts = Callable[[tuple[str, ...]], Awaitable[str]]


@dataclass(frozen=True)
class ArtifactResolution:
    text: str
    resources: tuple[OutgoingResource, ...]
    warnings: tuple[str, ...]
    repair_attempted: bool
    verified: bool


async def resolve_artifacts(
    initial_text: str,
    *,
    inspect: InspectArtifacts,
    repair: RepairArtifacts | None = None,
    max_items: int,
    max_total_bytes: int,
) -> ArtifactResolution:
    first = inspect(initial_text)
    if first.attempted == 0 or first.unresolved == 0:
        return ArtifactResolution(
            first.clean_text,
            first.resources,
            first.warnings,
            False,
            first.unresolved == 0,
        )
    if repair is None:
        return ArtifactResolution(first.clean_text, first.resources, first.warnings, False, False)

    repaired = inspect(await repair(first.warnings))
    merged, budget_ok, repair_contributed = _merge_resources(
        first.resources,
        repaired.resources,
        max_items=max_items,
        max_total_bytes=max_total_bytes,
    )
    return ArtifactResolution(
        first.clean_text,
        merged,
        repaired.warnings,
        True,
        repaired.unresolved == 0 and repair_contributed and budget_ok,
    )


def _merge_resources(
    first: tuple[OutgoingResource, ...],
    repaired: tuple[OutgoingResource, ...],
    *,
    max_items: int,
    max_total_bytes: int,
) -> tuple[tuple[OutgoingResource, ...], bool, bool]:
    merged: list[OutgoingResource] = []
    seen: set[tuple[str, str]] = set()
    total = 0
    repair_contributed = False
    for resources, from_repair in ((first, False), (repaired, True)):
        for resource in resources:
            source = resource.source_path or resource.path
            key = resource.kind, str(source.resolve(strict=False))
            if key in seen:
                continue
            if len(merged) >= max(0, max_items) or total + resource.size_bytes > max_total_bytes:
                return tuple(merged), False, repair_contributed
            seen.add(key)
            merged.append(resource)
            total += resource.size_bytes
            repair_contributed = repair_contributed or from_repair
    return tuple(merged), True, repair_contributed
