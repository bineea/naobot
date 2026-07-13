from __future__ import annotations

import struct

import pytest
from agentscope.message import DataBlock

from naobot.media.backends import build_vision_input_blocks
from naobot.media.pipeline import MediaPipeline
from naobot.media.protocol import (
    AUDIO_CAPABILITY,
    DEFAULT_MEDIA_CAPABILITIES,
    JPEG_CAPABILITY,
    NOMINAL_EVENT_VIDEO_FPS,
    NOMINAL_VIDEO_FPS,
    PCM16_MONO_16K_CAPABILITY,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    QVGA_CAPABILITY,
    MediaFrame,
    MediaFrameKind,
    MediaHello,
)


def test_media_frame_roundtrip_and_strict_decode() -> None:
    frame = MediaFrame(
        kind=MediaFrameKind.AUDIO_PCM16,
        timestamp_ms=12_345,
        sequence=7,
        payload=b"\x01\x02\x03\x04",
        flags=3,
    )

    encoded = frame.encode()
    decoded = MediaFrame.decode(encoded)

    assert PROTOCOL_HEADER.size == 24
    assert encoded[:4] == b"NABM"
    assert decoded == frame

    magic = bytearray(encoded)
    magic[0:4] = b"NOPE"
    with pytest.raises(ValueError, match="magic"):
        MediaFrame.decode(bytes(magic))

    version = bytearray(encoded)
    version[4] = PROTOCOL_VERSION + 1
    with pytest.raises(ValueError, match="version"):
        MediaFrame.decode(bytes(version))

    unknown_kind = bytearray(encoded)
    unknown_kind[5] = 99
    with pytest.raises(ValueError, match="kind"):
        MediaFrame.decode(bytes(unknown_kind))

    truncated = encoded[:-1]
    with pytest.raises(ValueError, match="length"):
        MediaFrame.decode(truncated)

    wrong_payload_length = bytearray(encoded)
    wrong_payload_length[8:12] = struct.pack(">I", 99)
    with pytest.raises(ValueError, match="length"):
        MediaFrame.decode(bytes(wrong_payload_length))


def test_media_hello_defaults_publish_nominal_capabilities() -> None:
    hello = MediaHello(device_id="robot-1", token="secret", boot_id="boot-1")

    assert hello.capabilities["video"]["nominal_fps"] == NOMINAL_VIDEO_FPS
    assert hello.capabilities["video"]["event_fps"] == NOMINAL_EVENT_VIDEO_FPS
    assert hello.capabilities["video"]["resolution"] == QVGA_CAPABILITY
    assert hello.capabilities["audio"] == AUDIO_CAPABILITY
    assert hello.capabilities["audio"]["format"] == PCM16_MONO_16K_CAPABILITY
    assert hello.capabilities["image"] == JPEG_CAPABILITY
    assert hello.capabilities == DEFAULT_MEDIA_CAPABILITIES


def test_media_pipeline_trims_windows_by_timestamp() -> None:
    pipeline = MediaPipeline(video_window_ms=10_000, audio_window_ms=15_000)

    pipeline.push_video_frame(MediaFrame.jpeg(b"a", timestamp_ms=0, sequence=1))
    pipeline.push_video_frame(MediaFrame.jpeg(b"b", timestamp_ms=5_000, sequence=2))
    pipeline.push_video_frame(MediaFrame.jpeg(b"c", timestamp_ms=11_001, sequence=3))

    pipeline.push_audio_chunk(
        MediaFrame.audio_pcm16(b"a", timestamp_ms=0, sequence=1),
        is_speech=False,
    )
    pipeline.push_audio_chunk(
        MediaFrame.audio_pcm16(b"b", timestamp_ms=10_000, sequence=2),
        is_speech=True,
    )
    pipeline.push_audio_chunk(
        MediaFrame.audio_pcm16(b"c", timestamp_ms=16_001, sequence=3),
        is_speech=True,
    )

    assert [frame.sequence for frame in pipeline.video_window()] == [2, 3]
    assert [chunk.frame.sequence for chunk in pipeline.audio_window()] == [2, 3]


def test_media_pipeline_enforces_backpressure_and_exposes_stats() -> None:
    pipeline = MediaPipeline(
        video_queue_limit=2,
        audio_queue_limit=3,
        video_window_ms=10_000,
        audio_window_ms=15_000,
    )

    pipeline.update_connection(True)
    pipeline.update_session("visitor-1", person_id="visitor", trigger="touch")

    pipeline.push_video_frame(MediaFrame.jpeg(b"1", timestamp_ms=100, sequence=1))
    pipeline.push_video_frame(MediaFrame.jpeg(b"2", timestamp_ms=200, sequence=2))
    pipeline.push_video_frame(MediaFrame.jpeg(b"3", timestamp_ms=300, sequence=3))

    pipeline.push_audio_chunk(
        MediaFrame.audio_pcm16(b"1", timestamp_ms=100, sequence=1),
        is_speech=False,
    )
    pipeline.push_audio_chunk(
        MediaFrame.audio_pcm16(b"2", timestamp_ms=200, sequence=2),
        is_speech=True,
    )
    pipeline.push_audio_chunk(
        MediaFrame.audio_pcm16(b"3", timestamp_ms=300, sequence=3),
        is_speech=False,
    )
    pipeline.push_audio_chunk(
        MediaFrame.audio_pcm16(b"4", timestamp_ms=400, sequence=4),
        is_speech=True,
    )
    pipeline.set_listening(True)
    pipeline.set_speaking(False)
    pipeline.set_last_transcript("你好")

    next_video = pipeline.next_video_frame()
    next_audio = pipeline.next_audio_chunk()
    stats = pipeline.stats()

    assert next_video is not None
    assert next_video.sequence == 2
    assert next_audio is not None
    assert next_audio.frame.sequence == 2
    assert [frame.sequence for frame in pipeline.video_queue()] == [3]
    assert [chunk.frame.sequence for chunk in pipeline.audio_queue()] == [3, 4]
    assert stats["connected"] is True
    assert stats["current_session"] == "visitor-1"
    assert stats["current_person"] == "visitor"
    assert stats["session_trigger"] == "touch"
    assert stats["audio_queue"] == 2
    assert stats["media_dropped"] == 2
    assert stats["listening"] is True
    assert stats["speaking"] is False
    assert stats["last_transcript"] == "你好"
    assert stats["video_fps"] > 0


def test_build_vision_input_blocks_keeps_at_most_three_jpegs() -> None:
    blocks = build_vision_input_blocks([b"1", b"2", b"3", b"4"])

    assert len(blocks) == 3
    assert all(isinstance(block, DataBlock) for block in blocks)
    assert [block.source.media_type for block in blocks] == ["image/jpeg"] * 3
