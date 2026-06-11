"""Hermes learning bridge (FR-LEARN-004/005, Section 16.3).

Writes ONLY compact, generalizable lessons marked for ACTIVE memory into a
``MEMORY.md``-style file. Raw logs and transcripts deliberately stay in the
audit store / session search, never in active memory. Also emits skill
candidates: reusable procedures Hermes may later promote to skills."""

from __future__ import annotations

from pathlib import Path

from hermes_pm.models import Lesson, MemoryTarget
from hermes_pm.util.timeutil import now_iso

_MAX_LESSON_CHARS = 320  # active memory must stay compact (FR-LEARN-004)


class HermesBridge:
    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "hermes"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _compact(self, lesson: Lesson) -> str:
        line = (
            f"- [{lesson.confidence:.2f}] WHEN {lesson.trigger} -> {lesson.rule}"
            f" (valid_until={lesson.valid_until or 'n/a'}; src={','.join(lesson.source_refs[:3])})"
        )
        return line[:_MAX_LESSON_CHARS]

    def export_active_memory(self, lessons: list[Lesson]) -> Path:
        active = [
            lesson for lesson in lessons if lesson.memory_target is MemoryTarget.ACTIVE
        ]
        path = self.dir / "MEMORY.md"
        body = [
            "# Hermes Active Memory — Prediction-Market Lessons",
            f"_generated {now_iso()} — compact, generalizable rules only_",
            "",
            *(self._compact(lesson) for lesson in active),
            "",
        ]
        path.write_text("\n".join(body), encoding="utf-8")
        return path

    def export_skill_candidate(
        self, name: str, description: str, steps: list[str], source_refs: list[str]
    ) -> Path:
        path = self.dir / f"skill_candidate_{name}.md"
        body = [
            f"# Skill Candidate: {name}",
            f"_generated {now_iso()}_",
            "",
            "## Description",
            description,
            "",
            "## Procedure",
            *(f"{i}. {s}" for i, s in enumerate(steps, 1)),
            "",
            "## Sources",
            *(f"- {r}" for r in source_refs),
            "",
            "_Promote to a Hermes skill only after repeated successful use._",
        ]
        path.write_text("\n".join(body), encoding="utf-8")
        return path
