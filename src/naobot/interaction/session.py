from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SessionSnapshot:
    active: bool
    session_id: str | None
    person_id: str | None
    session_trigger: str | None
    listening: bool
    speaking: bool
    barge_in: bool


class InteractionSession:
    def __init__(
        self,
        *,
        session_idle_ms: int = 30_000,
        eye_contact_activation_ms: int = 1_500,
        tts_resume_delay_ms: int = 200,
        barge_in: bool = False,
    ) -> None:
        self.session_idle_ms = session_idle_ms
        self.eye_contact_activation_ms = eye_contact_activation_ms
        self.tts_resume_delay_ms = tts_resume_delay_ms
        self.barge_in = barge_in
        self._active = False
        self._session_id: str | None = None
        self._person_id: str | None = None
        self._session_trigger: str | None = None
        self._last_activity_ms: int | None = None
        self._speaking = False
        self._resume_listening_at_ms: int | None = None

    def activate_from_wake_word(self, *, now_ms: int, person_id: str | None) -> bool:
        self._activate(now_ms=now_ms, person_id=person_id, trigger="wake_word")
        return True

    def activate_from_touch(self, *, now_ms: int, person_id: str | None) -> bool:
        self._activate(now_ms=now_ms, person_id=person_id, trigger="touch")
        return True

    def activate_from_greeting(self, *, now_ms: int, person_id: str | None) -> bool:
        self._activate(now_ms=now_ms, person_id=person_id, trigger="greeting")
        return True

    def activate_from_eye_contact(
        self,
        *,
        now_ms: int,
        eye_contact_ms: int,
        person_id: str | None,
    ) -> bool:
        if eye_contact_ms < self.eye_contact_activation_ms:
            return False
        self._activate(now_ms=now_ms, person_id=person_id, trigger="eye_contact")
        return True

    def mark_activity(self, *, now_ms: int) -> None:
        if self._active:
            self._last_activity_ms = now_ms

    def start_tts(self, *, now_ms: int) -> None:
        if not self._active:
            return
        self._speaking = True
        self._resume_listening_at_ms = None

    def end_tts(self, *, now_ms: int) -> None:
        if not self._active:
            return
        self._speaking = False
        self._resume_listening_at_ms = now_ms + self.tts_resume_delay_ms

    def snapshot(self, *, now_ms: int) -> SessionSnapshot:
        self._expire_if_idle(now_ms)
        listening = (
            self._active
            and not self._speaking
            and (
                self._resume_listening_at_ms is None
                or now_ms >= self._resume_listening_at_ms
            )
        )
        return SessionSnapshot(
            active=self._active,
            session_id=self._session_id,
            person_id=self._person_id,
            session_trigger=self._session_trigger,
            listening=listening,
            speaking=self._active and self._speaking,
            barge_in=self.barge_in,
        )

    def _activate(self, *, now_ms: int, person_id: str | None, trigger: str) -> None:
        self._active = True
        self._person_id = person_id
        self._session_id = person_id or f"visitor-{now_ms}"
        self._session_trigger = trigger
        self._last_activity_ms = now_ms
        self._speaking = False
        self._resume_listening_at_ms = None

    def _expire_if_idle(self, now_ms: int) -> None:
        if not self._active or self._last_activity_ms is None:
            return
        if now_ms - self._last_activity_ms <= self.session_idle_ms:
            return
        self._active = False
        self._session_id = None
        self._person_id = None
        self._session_trigger = None
        self._speaking = False
        self._resume_listening_at_ms = None
