from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from naobot.models import Envelope, MessageType
from naobot.settings import Settings

from ..media.backends import (
    ASRProvider,
    IdentityProvider,
    MotionEstimator,
    OpenCVMotionEstimator,
    TTSProvider,
    VisionProvider,
    WakeWordProvider,
    build_vision_input_blocks,
    coerce_wake_word_result,
)
from ..media.pipeline import MediaPipeline
from ..media.protocol import MediaFrame
from .session import InteractionSession


@dataclass(slots=True)
class CompletedTurn:
    event: Envelope
    vision_blocks: list = field(default_factory=list)
    audio_frames: list[MediaFrame] = field(default_factory=list)
    video_frames: list[MediaFrame] = field(default_factory=list)
    single_person: bool = False


class InteractionOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        pipeline: MediaPipeline,
        session: InteractionSession,
        wake_word: WakeWordProvider,
        identity: IdentityProvider,
        asr: ASRProvider,
        vision: VisionProvider,
        tts: TTSProvider,
        motion_estimator: MotionEstimator | None = None,
    ) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.session = session
        self.wake_word = wake_word
        self.identity = identity
        self.asr = asr
        self.vision = vision
        self.tts = tts
        self.motion_estimator = motion_estimator or OpenCVMotionEstimator()
        self.last_temporal_summary: dict[str, object] | None = None
        self._last_temporal_summary_at_ms: int | None = None
        self._sync_stats(now_ms=0)

    async def observe_audio(self, audio_frames: Sequence[MediaFrame], *, now_ms: int) -> None:
        self.pipeline.update_connection(True)
        for frame in audio_frames:
            self.pipeline.push_audio_chunk(frame)
        wake = coerce_wake_word_result(self.wake_word.detect(audio_frames))
        snapshot = self.session.snapshot(now_ms=now_ms)
        if wake.triggered:
            self.session.activate_from_wake_word(
                now_ms=now_ms,
                person_id=snapshot.person_id,
            )
        elif snapshot.active:
            self.session.mark_activity(now_ms=now_ms)
        elif wake.greeting_detected:
            self.session.activate_from_greeting(now_ms=now_ms, person_id=None)
        else:
            self.session.mark_activity(now_ms=now_ms)
        self._sync_stats(now_ms=now_ms)

    async def observe_video(self, video_frames: Sequence[MediaFrame], *, now_ms: int) -> None:
        self.pipeline.update_connection(True)
        for frame in video_frames:
            self.pipeline.push_video_frame(frame)
        identity = await asyncio.to_thread(self.identity.identify, video_frames)
        await self._update_temporal_summary(video_frames, identity.vision_summary, now_ms=now_ms)
        snapshot = self.session.snapshot(now_ms=now_ms)
        if not snapshot.active:
            if identity.eye_contact_ms >= self.session.eye_contact_activation_ms:
                self.session.activate_from_eye_contact(
                    now_ms=now_ms,
                    eye_contact_ms=identity.eye_contact_ms,
                    person_id=identity.person_id,
                )
            elif identity.greeting_detected:
                self.session.activate_from_greeting(now_ms=now_ms, person_id=identity.person_id)
        elif self._switch_known_person(
            current_person_id=snapshot.person_id,
            detected_person_id=identity.person_id,
            now_ms=now_ms,
        ):
            pass
        elif (
            snapshot.person_id is None
            and identity.person_id is not None
            and identity.eye_contact_ms >= self.session.eye_contact_activation_ms
        ):
            self.session.activate_from_eye_contact(
                now_ms=now_ms,
                eye_contact_ms=identity.eye_contact_ms,
                person_id=identity.person_id,
            )
        else:
            self.session.mark_activity(now_ms=now_ms)
        self._sync_stats(now_ms=now_ms)

    def observe_touch(self, *, now_ms: int, person_id: str | None = None) -> None:
        self.session.activate_from_touch(now_ms=now_ms, person_id=person_id)
        self._sync_stats(now_ms=now_ms)

    async def complete_turn(
        self,
        *,
        audio_frames: Sequence[MediaFrame],
        video_frames: Sequence[MediaFrame],
        now_ms: int,
        session_id: str | None = None,
        person_id: str | None = None,
        session_trigger: str | None = None,
    ) -> CompletedTurn | None:
        self.pipeline.update_connection(True)
        for frame in audio_frames:
            self.pipeline.push_audio_chunk(frame)
        for frame in video_frames:
            self.pipeline.push_video_frame(frame)

        bound_to_ingress_session = session_id is not None
        if not bound_to_ingress_session:
            snapshot = self.session.snapshot(now_ms=now_ms)
            if not snapshot.active or not snapshot.listening:
                self._sync_stats(now_ms=now_ms)
                return None
            session_id = snapshot.session_id or "visitor-session"
            person_id = snapshot.person_id
            session_trigger = snapshot.session_trigger

        asr_result = await self.asr.transcribe(audio_frames)
        vision_result = await self.vision.summarize(video_frames)
        identity_result = await asyncio.to_thread(self.identity.identify, video_frames)
        if not bound_to_ingress_session:
            if self._switch_known_person(
                current_person_id=person_id,
                detected_person_id=identity_result.person_id,
                now_ms=now_ms,
            ):
                snapshot = self.session.snapshot(now_ms=now_ms)
                session_id = snapshot.session_id or "visitor-session"
                person_id = snapshot.person_id
                session_trigger = snapshot.session_trigger
            self.session.mark_activity(now_ms=now_ms)
        self.pipeline.set_last_transcript(asr_result.transcript)
        if not bound_to_ingress_session:
            self._sync_stats(now_ms=now_ms)

        media_refs = [
            f"media://jpeg/{index + 1}/{frame.sequence}-{frame.timestamp_ms}"
            for index, frame in enumerate(video_frames[:3])
        ]
        vision_blocks = build_vision_input_blocks([frame.payload for frame in video_frames[:3]])

        return CompletedTurn(
            event=Envelope(
                type=MessageType.EVENT,
                session_id=session_id,
                payload={
                    "name": "user_utterance",
                    "transcript": asr_result.transcript,
                    "person_id": person_id,
                    "vision_summary": vision_result.summary,
                    "media_refs": media_refs,
                    "session_trigger": session_trigger,
                },
            ),
            vision_blocks=vision_blocks,
            audio_frames=list(audio_frames),
            video_frames=list(video_frames),
            single_person=identity_result.vision_summary == "检测到单人",
        )

    async def complete_utterance(
        self,
        *,
        audio_frames: Sequence[MediaFrame],
        video_frames: Sequence[MediaFrame],
        now_ms: int,
    ) -> Envelope | None:
        turn = await self.complete_turn(
            audio_frames=audio_frames,
            video_frames=video_frames,
            now_ms=now_ms,
        )
        return turn.event if turn is not None else None

    async def speak_text(self, text: str, *, now_ms: int):
        snapshot = self.session.snapshot(now_ms=now_ms)
        if not snapshot.active:
            self._sync_stats(now_ms=now_ms)
            return None
        self.session.start_tts(now_ms=now_ms)
        self._sync_stats(now_ms=now_ms)
        return await self.tts.synthesize(text)

    def finish_tts(self, *, now_ms: int) -> None:
        self.session.end_tts(now_ms=now_ms)
        self._sync_stats(now_ms=now_ms)

    def _switch_known_person(
        self,
        *,
        current_person_id: str | None,
        detected_person_id: str | None,
        now_ms: int,
    ) -> bool:
        if current_person_id is None or detected_person_id is None:
            return False
        if current_person_id == detected_person_id:
            return False
        return self.session.switch_person(now_ms=now_ms, person_id=detected_person_id)

    def _sync_stats(self, *, now_ms: int) -> None:
        snapshot = self.session.snapshot(now_ms=now_ms)
        self.pipeline.update_session(
            snapshot.session_id,
            person_id=snapshot.person_id,
            trigger=snapshot.session_trigger,
        )
        self.pipeline.set_listening(snapshot.listening)
        self.pipeline.set_speaking(snapshot.speaking)

    async def _update_temporal_summary(
        self,
        video_frames: Sequence[MediaFrame],
        scene_summary: str,
        *,
        now_ms: int,
    ) -> None:
        if not video_frames:
            return
        interval_ms = max(1, self.settings.temporal_summary_interval_ms)
        if (
            self._last_temporal_summary_at_ms is not None
            and now_ms - self._last_temporal_summary_at_ms < interval_ms
        ):
            return
        payload = video_frames[-1].payload
        motion = await asyncio.to_thread(self.motion_estimator.estimate, payload)
        self.last_temporal_summary = {
            "timestamp_ms": now_ms,
            "motion_score": motion.score,
            "method": motion.method,
            "scene_summary": scene_summary or "未检测到稳定场景",
        }
        self._last_temporal_summary_at_ms = now_ms
