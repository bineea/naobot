from __future__ import annotations

import copy
import struct
from dataclasses import dataclass, field
from enum import IntEnum

PROTOCOL_MAGIC = b"NABM"
PROTOCOL_VERSION = 1
PROTOCOL_HEADER = struct.Struct(">4sBBHIQI")
MAX_AUDIO_PCM16_PAYLOAD = 64 * 1024
MAX_JPEG_PAYLOAD = 256 * 1024
MAX_TTS_PCM16_PAYLOAD = 256 * 1024

NOMINAL_VIDEO_FPS = 10
NOMINAL_EVENT_VIDEO_FPS = 15
QVGA_CAPABILITY = {"width": 320, "height": 240}
PCM16_MONO_16K_CAPABILITY = {
    "sample_rate_hz": 16_000,
    "channels": 1,
    "encoding": "pcm16",
}
AUDIO_CAPABILITY = {"format": PCM16_MONO_16K_CAPABILITY}
JPEG_CAPABILITY = {"encoding": "jpeg"}
DEFAULT_MEDIA_CAPABILITIES = {
    "video": {
        "nominal_fps": NOMINAL_VIDEO_FPS,
        "event_fps": NOMINAL_EVENT_VIDEO_FPS,
        "resolution": QVGA_CAPABILITY,
    },
    "audio": AUDIO_CAPABILITY,
    "image": JPEG_CAPABILITY,
}


class MediaFrameKind(IntEnum):
    AUDIO_PCM16 = 1
    JPEG = 2
    TTS_PCM16 = 3


MAX_PAYLOAD_BYTES_BY_KIND = {
    MediaFrameKind.AUDIO_PCM16: MAX_AUDIO_PCM16_PAYLOAD,
    MediaFrameKind.JPEG: MAX_JPEG_PAYLOAD,
    MediaFrameKind.TTS_PCM16: MAX_TTS_PCM16_PAYLOAD,
}


@dataclass(slots=True)
class MediaFrame:
    kind: MediaFrameKind
    timestamp_ms: int
    sequence: int
    payload: bytes
    flags: int = 0

    def __post_init__(self) -> None:
        self.kind = MediaFrameKind(self.kind)
        if self.timestamp_ms < 0:
            raise ValueError("timestamp_ms must be non-negative")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.sequence > 0xFFFFFFFF:
            raise ValueError("sequence must fit into uint32")
        if self.timestamp_ms > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("timestamp_ms must fit into uint64")
        if self.flags < 0 or self.flags > 0xFFFF:
            raise ValueError("flags must fit into uint16")
        if len(self.payload) > 0xFFFFFFFF:
            raise ValueError("payload length exceeds uint32")
        limit = MAX_PAYLOAD_BYTES_BY_KIND[self.kind]
        if len(self.payload) > limit:
            raise ValueError(f"{self.kind.name} payload exceeds {limit} bytes")

    def encode(self) -> bytes:
        header = PROTOCOL_HEADER.pack(
            PROTOCOL_MAGIC,
            PROTOCOL_VERSION,
            int(self.kind),
            self.flags,
            self.sequence,
            self.timestamp_ms,
            len(self.payload),
        )
        return header + self.payload

    @classmethod
    def decode(cls, raw: bytes) -> MediaFrame:
        if len(raw) < PROTOCOL_HEADER.size:
            raise ValueError("frame length is smaller than header length")
        magic, version, raw_kind, flags, sequence, timestamp_ms, payload_length = (
            PROTOCOL_HEADER.unpack(raw[: PROTOCOL_HEADER.size])
        )
        if magic != PROTOCOL_MAGIC:
            raise ValueError("invalid frame magic")
        if version != PROTOCOL_VERSION:
            raise ValueError("unsupported frame version")
        try:
            kind = MediaFrameKind(raw_kind)
        except ValueError as exc:
            raise ValueError("invalid frame kind") from exc
        limit = MAX_PAYLOAD_BYTES_BY_KIND[kind]
        if payload_length > limit:
            raise ValueError(f"{kind.name} payload exceeds {limit} bytes")

        payload = raw[PROTOCOL_HEADER.size :]
        if len(payload) != payload_length:
            raise ValueError("frame payload length mismatch")
        return cls(
            kind=kind,
            timestamp_ms=timestamp_ms,
            sequence=sequence,
            payload=payload,
            flags=flags,
        )

    @property
    def is_speech(self) -> bool:
        return bool(self.flags & 0x1)

    @property
    def is_end_of_utterance(self) -> bool:
        return bool(self.flags & 0x2)

    @property
    def event_boosted(self) -> bool:
        return bool(self.flags & 0x4)

    @classmethod
    def audio_pcm16(
        cls,
        payload: bytes,
        *,
        timestamp_ms: int,
        sequence: int,
        flags: int = 0,
    ) -> MediaFrame:
        return cls(
            kind=MediaFrameKind.AUDIO_PCM16,
            timestamp_ms=timestamp_ms,
            sequence=sequence,
            payload=payload,
            flags=flags,
        )

    @classmethod
    def jpeg(
        cls,
        payload: bytes,
        *,
        timestamp_ms: int,
        sequence: int,
        flags: int = 0,
    ) -> MediaFrame:
        return cls(
            kind=MediaFrameKind.JPEG,
            timestamp_ms=timestamp_ms,
            sequence=sequence,
            payload=payload,
            flags=flags,
        )

    @classmethod
    def tts_pcm16(
        cls,
        payload: bytes,
        *,
        timestamp_ms: int,
        sequence: int,
        flags: int = 0,
    ) -> MediaFrame:
        return cls(
            kind=MediaFrameKind.TTS_PCM16,
            timestamp_ms=timestamp_ms,
            sequence=sequence,
            payload=payload,
            flags=flags,
        )


@dataclass(slots=True)
class MediaHello:
    device_id: str
    token: str
    boot_id: str
    capabilities: dict[str, object] = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_MEDIA_CAPABILITIES)
    )
