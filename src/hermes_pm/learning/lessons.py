"""Lesson service (FR-LEARN-003/006, Section 16.3).

Lessons are compact and structured: trigger, observation, pattern, rule,
valid-until, source refs. A lesson may only be promoted to ACTIVE memory if it
is supported by repeated evidence or explicit human confirmation — a single
lucky/unlucky trade can never become a durable rule (FR-LEARN-006)."""

from __future__ import annotations

from hermes_pm.events import EventBus, EventType
from hermes_pm.models import Lesson, MemoryTarget
from hermes_pm.persistence.db import Database

MIN_EVIDENCE_FOR_ACTIVE = 2


class LessonService:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus

    def create(
        self,
        campaign_id: str,
        *,
        trigger: str,
        observation: str,
        rule: str,
        pattern: str = "",
        confidence: float = 0.5,
        valid_until: str | None = None,
        source_refs: list[str] | None = None,
        memory_target: MemoryTarget = MemoryTarget.SESSION,
        supporting_evidence_count: int = 1,
        human_confirmed: bool = False,
    ) -> Lesson:
        downgrade_note = None
        target = memory_target
        # FR-LEARN-006: guard against promoting one-off results to durable rules.
        if target is MemoryTarget.ACTIVE and not (
            human_confirmed or supporting_evidence_count >= MIN_EVIDENCE_FOR_ACTIVE
        ):
            target = MemoryTarget.SESSION
            downgrade_note = (
                "downgraded from ACTIVE: needs repeated evidence or human confirmation"
            )

        lesson = Lesson(
            campaign_id=campaign_id,
            trigger=trigger,
            observation=observation,
            pattern=pattern,
            rule=rule,
            confidence=confidence,
            valid_until=valid_until,
            source_refs=source_refs or [],
            memory_target=target,
            supporting_evidence_count=supporting_evidence_count,
        )
        self.db.save_lesson(lesson)
        self.bus.publish(
            EventType.LESSON,
            {"lesson_id": lesson.lesson_id, "memory_target": target.value,
             "rule": rule, "downgrade_note": downgrade_note},
        )
        return lesson

    def reinforce(self, lesson_id: str) -> Lesson | None:
        """Add supporting evidence to an existing lesson; may make it promotable."""
        for lesson in self.db.list_lessons():
            if lesson.lesson_id == lesson_id:
                lesson.supporting_evidence_count += 1
                self.db.save_lesson(lesson)
                return lesson
        return None

    def list(self, campaign_id: str | None = None) -> list[Lesson]:
        return self.db.list_lessons(campaign_id)
