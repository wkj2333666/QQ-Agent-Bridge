"""Tests for the bounded artifact repair coordinator."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.artifact_delivery import resolve_artifacts  # type: ignore
from qq_agent_bridge.outgoing_resources import (  # type: ignore
    ArtifactInspection,
    OutgoingResource,
)


def make_resource(
    tmp_path: Path,
    name: str = "report.pdf",
    payload: bytes = b"pdf",
) -> OutgoingResource:
    source = tmp_path / name
    source.write_bytes(payload)
    return OutgoingResource(
        kind="file",
        path=source,
        name=name,
        source_path=source,
        size_bytes=len(payload),
    )


def inspection(
    *,
    text: str = "",
    resources: tuple[OutgoingResource, ...] = (),
    warnings: tuple[str, ...] = (),
    attempted: int = 0,
    unresolved: int = 0,
) -> ArtifactInspection:
    return ArtifactInspection(text, resources, warnings, attempted, unresolved, 0)


def test_resolution_skips_repair_for_verified_artifact(tmp_path: Path) -> None:
    async def go() -> None:
        calls = 0

        async def repair(_warnings: tuple[str, ...]) -> str:
            nonlocal calls
            calls += 1
            return "unused"

        result = await resolve_artifacts(
            "initial",
            inspect=lambda _text: inspection(
                text="完成", resources=(make_resource(tmp_path),), attempted=1
            ),
            repair=repair,
            max_items=4,
            max_total_bytes=1024,
        )

        assert result.verified is True
        assert result.repair_attempted is False
        assert calls == 0

    asyncio.run(go())


def test_resolution_repairs_unresolved_artifact_once(tmp_path: Path) -> None:
    async def go() -> None:
        inspected: list[str] = []

        def inspect(text: str) -> ArtifactInspection:
            inspected.append(text)
            if text == "repair-output":
                return inspection(resources=(make_resource(tmp_path),), attempted=1)
            return inspection(warnings=("missing",), attempted=1, unresolved=1)

        async def repair(warnings: tuple[str, ...]) -> str:
            assert warnings == ("missing",)
            return "repair-output"

        result = await resolve_artifacts(
            "initial",
            inspect=inspect,
            repair=repair,
            max_items=4,
            max_total_bytes=1024,
        )

        assert result.verified is True
        assert result.repair_attempted is True
        assert inspected == ["initial", "repair-output"]

    asyncio.run(go())


def test_resolution_requires_one_new_resource_per_originally_unresolved_directive(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        repaired = make_resource(tmp_path, "first.pdf")

        result = await resolve_artifacts(
            "initial",
            inspect=lambda text: (
                inspection(resources=(repaired,), attempted=1)
                if text == "repair-output"
                else inspection(
                    warnings=("first missing", "second missing"),
                    attempted=2,
                    unresolved=2,
                )
            ),
            repair=lambda _warnings: _completed("repair-output"),
            max_items=4,
            max_total_bytes=1024,
        )

        assert result.resources == (repaired,)
        assert result.repair_attempted is True
        assert result.verified is False

    asyncio.run(go())


def test_resolution_verifies_when_repair_covers_every_originally_unresolved_directive(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        first = make_resource(tmp_path, "first.pdf")
        second = make_resource(tmp_path, "second.pdf")

        result = await resolve_artifacts(
            "initial",
            inspect=lambda text: (
                inspection(resources=(first, second), attempted=2)
                if text == "repair-output"
                else inspection(
                    warnings=("first missing", "second missing"),
                    attempted=2,
                    unresolved=2,
                )
            ),
            repair=lambda _warnings: _completed("repair-output"),
            max_items=4,
            max_total_bytes=1024,
        )

        assert result.resources == (first, second)
        assert result.repair_attempted is True
        assert result.verified is True

    asyncio.run(go())


def test_resolution_does_not_verify_empty_repair_from_initial_resources(tmp_path: Path) -> None:
    async def go() -> None:
        first = make_resource(tmp_path)

        def inspect(text: str) -> ArtifactInspection:
            if text == "initial":
                return inspection(
                    resources=(first,),
                    warnings=("missing",),
                    attempted=2,
                    unresolved=1,
                )
            return inspection()

        result = await resolve_artifacts(
            "initial",
            inspect=inspect,
            repair=lambda _warnings: _completed(""),
            max_items=4,
            max_total_bytes=1024,
        )

        assert result.resources == (first,)
        assert result.repair_attempted is True
        assert result.verified is False

    asyncio.run(go())


def test_resolution_keeps_attempted_zero_text_only_output_verified() -> None:
    async def go() -> None:
        calls = 0

        async def repair(_warnings: tuple[str, ...]) -> str:
            nonlocal calls
            calls += 1
            return "unused"

        result = await resolve_artifacts(
            "plain text",
            inspect=lambda _text: inspection(text="plain text"),
            repair=repair,
            max_items=4,
            max_total_bytes=1024,
        )

        assert result.text == "plain text"
        assert result.resources == ()
        assert result.repair_attempted is False
        assert result.verified is True
        assert calls == 0

    asyncio.run(go())


def test_resolution_does_not_verify_repair_with_zero_merged_resources() -> None:
    async def go() -> None:
        result = await resolve_artifacts(
            "initial",
            inspect=lambda text: (
                inspection(warnings=("missing",), attempted=1, unresolved=1)
                if text == "initial"
                else inspection()
            ),
            repair=lambda _warnings: _completed(""),
            max_items=4,
            max_total_bytes=1024,
        )

        assert result.resources == ()
        assert result.repair_attempted is True
        assert result.verified is False

    asyncio.run(go())


def test_resolution_never_repairs_failed_repair_twice() -> None:
    async def go() -> None:
        calls = 0

        async def repair(_warnings: tuple[str, ...]) -> str:
            nonlocal calls
            calls += 1
            return "still-missing"

        result = await resolve_artifacts(
            "initial",
            inspect=lambda _text: inspection(warnings=("missing",), attempted=1, unresolved=1),
            repair=repair,
            max_items=4,
            max_total_bytes=1024,
        )

        assert result.verified is False
        assert result.repair_attempted is True
        assert calls == 1

    asyncio.run(go())


def test_resolution_deduplicates_repair_resources(tmp_path: Path) -> None:
    async def go() -> None:
        first = make_resource(tmp_path, "first.pdf")
        second = make_resource(tmp_path, "second.pdf")

        def inspect(text: str) -> ArtifactInspection:
            if text == "repair-output":
                return inspection(resources=(first, second), attempted=1)
            return inspection(
                text="initial text",
                resources=(first,),
                warnings=("missing",),
                attempted=1,
                unresolved=1,
            )

        result = await resolve_artifacts(
            "initial",
            inspect=inspect,
            repair=lambda _warnings: _completed("repair-output"),
            max_items=4,
            max_total_bytes=1024,
        )

        assert result.text == "initial text"
        assert result.resources == (first, second)
        assert result.verified is True

    asyncio.run(go())


def test_resolution_does_not_count_same_source_with_new_kind_as_repair(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        first = make_resource(tmp_path, "plot.png", b"png")
        repaired_alias = OutgoingResource(
            kind="image",
            path=first.path,
            name=first.name,
            source_path=first.source_path,
            size_bytes=first.size_bytes,
        )

        def inspect(text: str) -> ArtifactInspection:
            if text == "repair-output":
                return inspection(resources=(repaired_alias,), attempted=1)
            return inspection(
                resources=(first,),
                warnings=("missing",),
                attempted=2,
                unresolved=1,
            )

        result = await resolve_artifacts(
            "initial",
            inspect=inspect,
            repair=lambda _warnings: _completed("repair-output"),
            max_items=4,
            max_total_bytes=1024,
        )

        assert result.resources == (first,)
        assert result.repair_attempted is True
        assert result.verified is False

    asyncio.run(go())


def test_resolution_rejects_merged_item_count_over_budget(tmp_path: Path) -> None:
    async def go() -> None:
        first = make_resource(tmp_path, "first.pdf")
        second = make_resource(tmp_path, "second.pdf")

        def inspect(text: str) -> ArtifactInspection:
            if text == "repair-output":
                return inspection(resources=(second,), attempted=1)
            return inspection(
                resources=(first,), warnings=("missing",), attempted=1, unresolved=1
            )

        result = await resolve_artifacts(
            "initial",
            inspect=inspect,
            repair=lambda _warnings: _completed("repair-output"),
            max_items=1,
            max_total_bytes=1024,
        )

        assert result.resources == (first,)
        assert result.verified is False

    asyncio.run(go())


def test_resolution_rejects_merged_total_size_over_budget(tmp_path: Path) -> None:
    async def go() -> None:
        first = make_resource(tmp_path, "first.pdf", b"one")
        second = make_resource(tmp_path, "second.pdf", b"two")

        def inspect(text: str) -> ArtifactInspection:
            if text == "repair-output":
                return inspection(resources=(second,), attempted=1)
            return inspection(
                resources=(first,), warnings=("missing",), attempted=1, unresolved=1
            )

        result = await resolve_artifacts(
            "initial",
            inspect=inspect,
            repair=lambda _warnings: _completed("repair-output"),
            max_items=4,
            max_total_bytes=5,
        )

        assert result.resources == (first,)
        assert result.verified is False

    asyncio.run(go())


def test_resolution_propagates_repair_cancellation() -> None:
    async def go() -> None:
        calls = 0

        async def repair(_warnings: tuple[str, ...]) -> str:
            nonlocal calls
            calls += 1
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await resolve_artifacts(
                "initial",
                inspect=lambda _text: inspection(
                    warnings=("missing",), attempted=1, unresolved=1
                ),
                repair=repair,
                max_items=4,
                max_total_bytes=1024,
            )
        assert calls == 1

    asyncio.run(go())


async def _completed(value: str) -> str:
    return value
