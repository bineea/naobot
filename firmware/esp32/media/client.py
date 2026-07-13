import json

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

try:
    import utime as time
except ImportError:
    import time

from media.devices import AudioInput, AudioOutput, Camera
from media.protocol import (
    FLAG_END_OF_UTTERANCE,
    FLAG_EVENT_BOOST,
    FLAG_SPEECH,
    KIND_AUDIO_PCM16,
    KIND_JPEG,
    KIND_TTS_PCM16,
    MediaFrame,
)
from media.websocket import OP_BINARY, OP_TEXT, MediaWebSocket

NORMAL_VIDEO_FPS = 10
EVENT_VIDEO_FPS = 15
NORMAL_VIDEO_INTERVAL_MS = 100
EVENT_VIDEO_INTERVAL_MS = 67
TTS_WRITE_CHUNK_BYTES = 1024

MEDIA_CAPABILITIES = {
    "video": {
        "nominal_fps": NORMAL_VIDEO_FPS,
        "event_fps": EVENT_VIDEO_FPS,
        "resolution": {"width": 320, "height": 240},
    },
    "audio": {
        "format": {"sample_rate_hz": 16000, "channels": 1, "encoding": "pcm16"}
    },
    "image": {"encoding": "jpeg"},
}


def now_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def ticks_diff(end, start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(end, start)
    return end - start


async def sleep_ms(delay_ms):
    if hasattr(asyncio, "sleep_ms"):
        await asyncio.sleep_ms(delay_ms)
    else:
        await asyncio.sleep(delay_ms / 1000)


class VideoScheduler:
    def __init__(self):
        self.last_capture_ms = None

    def reset(self):
        self.last_capture_ms = None

    def should_capture(self, current_ms, event_boost=False):
        interval = EVENT_VIDEO_INTERVAL_MS if event_boost else NORMAL_VIDEO_INTERVAL_MS
        if self.last_capture_ms is None or ticks_diff(current_ms, self.last_capture_ms) >= interval:
            self.last_capture_ms = current_ms
            return True
        return False


class MediaQueue:
    def __init__(self, max_items=12):
        self.max_items = max(1, max_items)
        self._items = []
        self.dropped_video = 0
        self.dropped_audio = 0
        self.dropped_total = 0

    def __len__(self):
        return len(self._items)

    def items(self):
        return list(self._items)

    def put(self, frame):
        if len(self._items) >= self.max_items:
            drop_index = self._find_drop_candidate(KIND_JPEG)
            if drop_index is None:
                drop_index = self._find_non_speech_audio()
            if drop_index is None:
                self._record_drop(frame)
                return False
            dropped = self._items.pop(drop_index)
            self._record_drop(dropped)
        self._items.append(frame)
        return True

    def pop(self):
        if not self._items:
            return None
        return self._items.pop(0)

    def put_front(self, frame):
        self._items.insert(0, frame)

    def _find_drop_candidate(self, kind):
        for index, frame in enumerate(self._items):
            if frame.kind == kind:
                return index
        return None

    def _find_non_speech_audio(self):
        for index, frame in enumerate(self._items):
            if frame.kind == KIND_AUDIO_PCM16 and not frame.is_speech:
                return index
        return None

    def _record_drop(self, frame):
        self.dropped_total += 1
        if frame.kind == KIND_JPEG:
            self.dropped_video += 1
        elif frame.kind == KIND_AUDIO_PCM16:
            self.dropped_audio += 1


class MediaClient:
    def __init__(
        self,
        url,
        device_id,
        token,
        boot_id,
        camera=None,
        audio_input=None,
        audio_output=None,
        transport_factory=None,
        state=None,
        queue_limit=12,
    ):
        self.url = url
        self.device_id = device_id
        self.token = token
        self.boot_id = boot_id
        self.camera = camera or Camera()
        self.audio_input = audio_input or AudioInput()
        self.audio_output = audio_output or AudioOutput()
        self.transport_factory = transport_factory or MediaWebSocket
        self.transport = None
        self.queue = MediaQueue(queue_limit)
        self.scheduler = VideoScheduler()
        self.sequence = 0
        self.state = state if state is not None else {}
        self.state.setdefault("media_connected", False)
        self.state.setdefault("camera_fps", 0)
        self.state.setdefault(
            "audio_state", "listening" if self.audio_input.available else "unavailable"
        )
        self.state.setdefault("media_queue", 0)
        self.state.setdefault("media_dropped", 0)
        self.state.setdefault("psram_free", self.camera.psram_free())
        self.state.setdefault("event_boost_until_ms", 0)
        self._camera_window_started_ms = None
        self._camera_frames = 0
        self._speaking = False
        self._tts_chunks = []
        self._tts_end_received = False

    def connect(self):
        if self.transport is None:
            self.transport = self.transport_factory(self.url)
        if not self.transport.connect():
            self._disconnect()
            return False
        hello = {
            "kind": "media_hello",
            "device_id": self.device_id,
            "token": self.token,
            "boot_id": self.boot_id,
            "capabilities": MEDIA_CAPABILITIES,
        }
        if not self.transport.send_text(json.dumps(hello)):
            self._disconnect()
            return False
        self.state["media_connected"] = True
        return True

    def step(self, current_ms=None):
        current_ms = now_ms() if current_ms is None else current_ms
        try:
            if self.transport is None or not self.transport.connected:
                if not self.connect():
                    return False
            incoming = self.transport.recv_frame()
            if incoming is not None:
                self.handle_incoming(incoming[0], incoming[1])
            if self._speaking:
                self._drain_tts_chunk()
            else:
                event_boost = ticks_diff(self.state.get("event_boost_until_ms", 0), current_ms) > 0
                audio_flags = 0
                if self.state.pop("audio_speech", False):
                    audio_flags |= FLAG_SPEECH
                if self.state.pop("audio_eou", False):
                    audio_flags |= FLAG_END_OF_UTTERANCE
                self.collect(current_ms, event_boost=event_boost, audio_flags=audio_flags)
                self.flush_one()
            self._update_state(current_ms)
            return True
        except Exception as exc:
            print("media client error:", exc)
            self._disconnect()
            self._update_state(current_ms)
            return False

    def collect(self, current_ms, event_boost=False, audio_flags=0):
        if self._speaking:
            return
        event_flag = FLAG_EVENT_BOOST if event_boost else 0
        if self.camera.available and self.scheduler.should_capture(current_ms, event_boost):
            payload = self.camera.capture()
            if payload:
                self.queue.put(
                    MediaFrame(
                        KIND_JPEG,
                        current_ms,
                        self._next_sequence(),
                        payload,
                        event_flag,
                    )
                )
                self._camera_frames += 1
        payload = self.audio_input.read_chunk()
        if payload:
            self.queue.put(
                MediaFrame(
                    KIND_AUDIO_PCM16,
                    current_ms,
                    self._next_sequence(),
                    payload,
                    audio_flags | event_flag,
                )
            )
        self._update_state(current_ms)

    def flush_one(self):
        frame = self.queue.pop()
        if frame is None:
            return True
        try:
            sent = self.transport.send_binary(frame.encode())
        except Exception:
            self.queue.put_front(frame)
            raise
        if not sent:
            self.queue.put_front(frame)
            raise OSError("media websocket send failed")
        return True

    def handle_incoming(self, opcode, payload):
        if opcode == OP_TEXT:
            if isinstance(payload, bytes):
                payload = payload.decode()
            message = json.loads(payload)
            kind = message.get("kind") if isinstance(message, dict) else None
            if kind == "tts_start":
                self._speaking = True
                self._tts_end_received = False
                self.state["audio_state"] = "speaking"
            elif kind == "tts_end":
                self._tts_end_received = True
                self._finish_tts_if_drained()
            return
        if opcode != OP_BINARY:
            return
        frame = MediaFrame.decode(payload)
        if frame.kind != KIND_TTS_PCM16:
            raise ValueError("unexpected downlink media kind")
        self._speaking = True
        self.state["audio_state"] = "speaking"
        self._tts_chunks.append(frame.payload)

    def _drain_tts_chunk(self):
        if not self._tts_chunks:
            self._finish_tts_if_drained()
            return
        payload = self._tts_chunks[0]
        chunk = payload[:TTS_WRITE_CHUNK_BYTES]
        written = self.audio_output.write(chunk)
        if written <= 0:
            written = len(chunk)
        remaining = payload[written:]
        if remaining:
            self._tts_chunks[0] = remaining
        else:
            self._tts_chunks.pop(0)
        self._finish_tts_if_drained()

    def _finish_tts_if_drained(self):
        if not self._tts_end_received or self._tts_chunks:
            return
        self._speaking = False
        self._tts_end_received = False
        self.state["audio_state"] = (
            "listening" if self.audio_input.available else "unavailable"
        )

    def _next_sequence(self):
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        return self.sequence

    def _update_state(self, current_ms):
        if self._camera_window_started_ms is None:
            self._camera_window_started_ms = current_ms
        elapsed = ticks_diff(current_ms, self._camera_window_started_ms)
        if elapsed >= 1000:
            self.state["camera_fps"] = int((self._camera_frames * 1000 + elapsed // 2) // elapsed)
            self._camera_frames = 0
            self._camera_window_started_ms = current_ms
        self.state["media_queue"] = len(self.queue)
        self.state["media_dropped"] = self.queue.dropped_total
        self.state["psram_free"] = self.camera.psram_free()

    def _disconnect(self):
        if self.transport is not None:
            self.transport.close()
        self.transport = None
        self.state["media_connected"] = False


def create_media_client(state):
    from config import DEVICE_ID, DEVICE_TOKEN, MEDIA_QUEUE_LIMIT, MEDIA_WS_URL

    return MediaClient(
        MEDIA_WS_URL,
        device_id=DEVICE_ID,
        token=DEVICE_TOKEN,
        boot_id=f"boot-{now_ms()}",
        state=state,
        queue_limit=MEDIA_QUEUE_LIMIT,
    )


async def media_loop(state):
    from config import MEDIA_LOOP_INTERVAL_MS, MEDIA_RECONNECT_DELAY_MS

    client = create_media_client(state)
    while True:
        connected = client.step()
        await sleep_ms(MEDIA_LOOP_INTERVAL_MS if connected else MEDIA_RECONNECT_DELAY_MS)
