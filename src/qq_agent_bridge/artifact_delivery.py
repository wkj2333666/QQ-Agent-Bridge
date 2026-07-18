"""Coordinate one bounded artifact repair attempt."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .outgoing_resources import ArtifactExpectation, ArtifactInspection, OutgoingResource

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
    merged, budget_ok, repair_contributions = _merge_resources(
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
        repaired.unresolved == 0
        and len(first.unresolved_expectations) == first.unresolved
        and _repair_matches_expectations(
            first.unresolved_expectations,
            repair_contributions,
        )
        and budget_ok,
    )


def _merge_resources(
    first: tuple[OutgoingResource, ...],
    repaired: tuple[OutgoingResource, ...],
    *,
    max_items: int,
    max_total_bytes: int,
) -> tuple[tuple[OutgoingResource, ...], bool, tuple[OutgoingResource, ...]]:
    merged: list[OutgoingResource] = []
    seen: set[str] = set()
    total = 0
    repair_contributions: list[OutgoingResource] = []
    for resources, from_repair in ((first, False), (repaired, True)):
        for resource in resources:
            source = resource.source_path or resource.path
            key = str(source.resolve(strict=False))
            if key in seen:
                continue
            if len(merged) >= max(0, max_items) or total + resource.size_bytes > max_total_bytes:
                return tuple(merged), False, tuple(repair_contributions)
            seen.add(key)
            merged.append(resource)
            total += resource.size_bytes
            if from_repair:
                repair_contributions.append(resource)
    return tuple(merged), True, tuple(repair_contributions)


def _repair_matches_expectations(
    expectations: tuple[ArtifactExpectation, ...],
    resources: tuple[OutgoingResource, ...],
) -> bool:
    if not expectations or len(expectations) != len(resources):
        return False
    remaining = list(resources)
    ordered = sorted(expectations, key=lambda expectation: expectation.requested_basename is None)
    for expectation in ordered:
        match = next(
            (
                index
                for index, resource in enumerate(remaining)
                if _resource_matches_expectation(resource, expectation)
            ),
            None,
        )
        if match is None:
            return False
        remaining.pop(match)
    return not remaining


def _resource_matches_expectation(
    resource: OutgoingResource,
    expectation: ArtifactExpectation,
) -> bool:
    if resource.kind != expectation.kind:
        return False
    if expectation.requested_basename is None:
        return True
    source_name = resource.source_path.name if resource.source_path is not None else resource.name
    return source_name == expectation.requested_basename
