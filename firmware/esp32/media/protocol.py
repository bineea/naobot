try:
    import ustruct as struct
except ImportError:
    import struct

PROTOCOL_MAGIC = b"NABM"
PROTOCOL_VERSION = 1
HEADER_FORMAT = ">4sBBHIQI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

KIND_AUDIO_PCM16 = 1
KIND_JPEG = 2
KIND_TTS_PCM16 = 3

FLAG_SPEECH = 0x1
FLAG_END_OF_UTTERANCE = 0x2
FLAG_EVENT_BOOST = 0x4

MAX_AUDIO_PCM16_PAYLOAD = 64 * 1024
MAX_JPEG_PAYLOAD = 256 * 1024
MAX_TTS_PCM16_PAYLOAD = 256 * 1024

MAX_PAYLOAD_BY_KIND = {
    KIND_AUDIO_PCM16: MAX_AUDIO_PCM16_PAYLOAD,
    KIND_JPEG: MAX_JPEG_PAYLOAD,
    KIND_TTS_PCM16: MAX_TTS_PCM16_PAYLOAD,
}


class MediaFrame:
    __slots__ = ("kind", "timestamp_ms", "sequence", "payload", "flags")

    def __init__(self, kind, timestamp_ms, sequence, payload, flags=0):
        if kind not in MAX_PAYLOAD_BY_KIND:
            raise ValueError("invalid frame kind")
        if timestamp_ms < 0 or timestamp_ms > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("timestamp must fit into uint64")
        if sequence < 0 or sequence > 0xFFFFFFFF:
            raise ValueError("sequence must fit into uint32")
        if flags < 0 or flags > 0xFFFF:
            raise ValueError("flags must fit into uint16")
        payload = bytes(payload)
        if len(payload) > MAX_PAYLOAD_BY_KIND[kind]:
            raise ValueError("payload exceeds kind limit")
        self.kind = kind
        self.timestamp_ms = timestamp_ms
        self.sequence = sequence
        self.payload = payload
        self.flags = flags

    def encode(self):
        return struct.pack(
            HEADER_FORMAT,
            PROTOCOL_MAGIC,
            PROTOCOL_VERSION,
            self.kind,
            self.flags,
            self.sequence,
            self.timestamp_ms,
            len(self.payload),
        ) + self.payload

    @classmethod
    def decode(cls, raw):
        if len(raw) < HEADER_SIZE:
            raise ValueError("frame length is smaller than header length")
        magic, version, kind, flags, sequence, timestamp_ms, payload_length = struct.unpack(
            HEADER_FORMAT, raw[:HEADER_SIZE]
        )
        if magic != PROTOCOL_MAGIC:
            raise ValueError("invalid frame magic")
        if version != PROTOCOL_VERSION:
            raise ValueError("unsupported frame version")
        if kind not in MAX_PAYLOAD_BY_KIND:
            raise ValueError("invalid frame kind")
        if payload_length > MAX_PAYLOAD_BY_KIND[kind]:
            raise ValueError("payload exceeds kind limit")
        payload = raw[HEADER_SIZE:]
        if len(payload) != payload_length:
            raise ValueError("frame payload length mismatch")
        return cls(kind, timestamp_ms, sequence, payload, flags)

    @property
    def is_speech(self):
        return bool(self.flags & FLAG_SPEECH)

    @property
    def is_end_of_utterance(self):
        return bool(self.flags & FLAG_END_OF_UTTERANCE)

    @property
    def event_boosted(self):
        return bool(self.flags & FLAG_EVENT_BOOST)
