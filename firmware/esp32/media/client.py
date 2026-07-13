import json

try:
    import utime as time
except ImportError:
    import time

from media.devices import AudioInput, AudioOutput, Camera
from media.protocol import (
    FLAG_EVENT_BOOST,
    KIND_AUDIO_PCM16,
    KIND_JPEG,
    KIND_TTS_PCM16,
    MediaFrame,
)
from media.vad import EnergyVAD
from media.websocket import OP_BINARY, OP_TEXT, MediaWebSocket

NORMAL_VIDEO_FPS = 10
EVENT_VIDEO_FPS = 15
NORMAL_VIDEO_INTERVAL_MS = 100
EVENT_VIDEO_INTERVAL_MS = 67
TTS_WRITE_CHUNK_BYTES = 1024
TTS_BUFFER_LIMIT_BYTES = 64 * 1024
TTS_PLAYBACK_TIMEOUT_MS = 30000

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
        vad=None,
        tts_buffer_limit_bytes=TTS_BUFFER_LIMIT_BYTES,
        tts_playback_timeout_ms=TTS_PLAYBACK_TIMEOUT_MS,
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
        self.vad = vad or EnergyVAD()
        self.tts_buffer_limit_bytes = max(1024, tts_buffer_limit_bytes)
        self.tts_playback_timeout_ms = max(100, tts_playback_timeout_ms)
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
        self.state.setdefault("tts_dropped", 0)
        self._camera_window_started_ms = None
        self._camera_frames = 0
        self._speaking = False
        self._tts_chunks = []
        self._tts_buffered_bytes = 0
        self._tts_end_received = False
        self._tts_last_progress_ms = None

    def connect(self):
        if self.transport is not None and self.transport.connected:
            return True
        transport = None
        try:
            transport = self._new_transport()
            if transport is None or not transport.connect():
                if transport is not None:
                    transport.close()
                return False
            return self._activate_transport(transport)
        except Exception as exc:
            print("media connect error:", exc)
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            self._disconnect()
            return False

    def _new_transport(self):
        return self.transport_factory(self.url)

    def _activate_transport(self, transport):
        self.transport = transport
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
                self.handle_incoming(incoming[0], incoming[1], current_ms=current_ms)
            if getattr(self.transport, "tx_pending", False):
                if not self.transport.flush_tx_chunk():
                    raise OSError("media websocket send failed")
            if self._tts_timed_out(current_ms):
                self._reset_tts()
            elif self._speaking:
                self._drain_tts_chunk(current_ms)
                event_boost = ticks_diff(self.state.get("event_boost_until_ms", 0), current_ms) > 0
                self.collect(current_ms, event_boost=event_boost)
                if not getattr(self.transport, "tx_pending", False):
                    self.flush_one()
            else:
                event_boost = ticks_diff(self.state.get("event_boost_until_ms", 0), current_ms) > 0
                self.collect(current_ms, event_boost=event_boost)
                if not getattr(self.transport, "tx_pending", False):
                    self.flush_one()
            self._update_state(current_ms)
            return True
        except Exception as exc:
            print("media client error:", exc)
            self._disconnect()
            self._update_state(current_ms)
            return False

    def collect(self, current_ms, event_boost=False, audio_flags=0):
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
        if self._speaking:
            self._update_state(current_ms)
            return
        payload = self.audio_input.read_chunk()
        if payload:
            audio_flags |= self.vad.process(payload)
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
        if getattr(self.transport, "tx_pending", False):
            return True
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

    def handle_incoming(self, opcode, payload, current_ms=None):
        current_ms = now_ms() if current_ms is None else current_ms
        if opcode == OP_TEXT:
            if isinstance(payload, bytes):
                payload = payload.decode()
            message = json.loads(payload)
            kind = message.get("kind") if isinstance(message, dict) else None
            if kind == "tts_start":
                self._start_tts(current_ms)
            elif kind == "tts_end":
                self._tts_end_received = True
                self._mark_tts_progress(current_ms)
                self._finish_tts_if_drained()
            return
        if opcode != OP_BINARY:
            return
        frame = MediaFrame.decode(payload)
        if frame.kind != KIND_TTS_PCM16:
            raise ValueError("unexpected downlink media kind")
        if not self._speaking:
            self._start_tts(current_ms)
        if self._tts_buffered_bytes + len(frame.payload) > self.tts_buffer_limit_bytes:
            self.state["tts_dropped"] += 1
            self._reset_tts()
            return
        self._tts_chunks.append(frame.payload)
        self._tts_buffered_bytes += len(frame.payload)
        self._mark_tts_progress(current_ms)

    def _drain_tts_chunk(self, current_ms):
        if not self._tts_chunks:
            self._finish_tts_if_drained()
            return
        payload = self._tts_chunks[0]
        chunk = payload[:TTS_WRITE_CHUNK_BYTES]
        written = self.audio_output.write(chunk)
        if written <= 0:
            return
        written = min(written, len(chunk))
        self._tts_buffered_bytes -= written
        self._mark_tts_progress(current_ms)
        remaining = payload[written:]
        if remaining:
            self._tts_chunks[0] = remaining
        else:
            self._tts_chunks.pop(0)
        self._finish_tts_if_drained()

    def _start_tts(self, current_ms):
        self.vad.reset()
        self._tts_chunks = []
        self._tts_buffered_bytes = 0
        self._tts_end_received = False
        self._tts_last_progress_ms = current_ms
        self._speaking = True
        self.state["audio_state"] = "speaking"

    def _tts_timed_out(self, current_ms):
        return (
            self._speaking
            and self._tts_last_progress_ms is not None
            and ticks_diff(current_ms, self._tts_last_progress_ms)
            >= self.tts_playback_timeout_ms
        )

    def _mark_tts_progress(self, current_ms):
        self._tts_last_progress_ms = current_ms

    def _reset_tts(self):
        self._speaking = False
        self._tts_chunks = []
        self._tts_buffered_bytes = 0
        self._tts_end_received = False
        self._tts_last_progress_ms = None
        self.state["audio_state"] = (
            "listening" if self.audio_input.available else "unavailable"
        )

    def _finish_tts_if_drained(self):
        if not self._tts_end_received or self._tts_chunks:
            return
        self._reset_tts()

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
            try:
                self.transport.close(send_close=False)
            except TypeError:
                self.transport.close()
        self.transport = None
        self.state["media_connected"] = False
        self._reset_tts()
        self.vad.reset()

    def close(self):
        self._disconnect()
        for device in (self.camera, self.audio_input, self.audio_output):
            close = getattr(device, "close", None)
            if close:
                try:
                    close()
                except Exception as exc:
                    print("media device close error:", exc)


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
