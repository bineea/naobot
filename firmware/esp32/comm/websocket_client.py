import json

try:
    import urandom as random
except ImportError:
    import random

try:
    import utime as time
except ImportError:
    import time

try:
    from config import WS_CONNECT_TIMEOUT_SEC, WS_SOCKET_TIMEOUT_SEC
except ImportError:
    WS_CONNECT_TIMEOUT_SEC = 2
    WS_SOCKET_TIMEOUT_SEC = 0.01

from media import websocket as websocket_protocol

OP_TEXT = websocket_protocol.OP_TEXT
MediaWebSocket = websocket_protocol.MediaWebSocket
MAX_CONTROL_FLUSH_CHUNKS = 4


def parse_ws_url(url):
    return websocket_protocol.parse_ws_url(url)


def _random_bytes(length):
    try:
        return random.urandom(length)
    except AttributeError:
        pass
    try:
        return bytes(random.getrandbits(8) for _ in range(length))
    except AttributeError:
        seed = int(time.time() * 1000)
        return bytes((seed + index * 37) & 0xFF for index in range(length))


class WebSocketClient(MediaWebSocket):
    """控制链路适配器，复用媒体链路的增量 RFC 6455 帧解析。"""

    def __init__(self, url, token=""):
        headers = {"X-Naobot-Token": token} if token else None
        super().__init__(
            url,
            io_timeout_sec=WS_SOCKET_TIMEOUT_SEC,
            connect_timeout_sec=WS_CONNECT_TIMEOUT_SEC,
            max_rx_bytes=64 * 1024,
            max_message_bytes=32 * 1024,
            headers=headers,
        )

    def connect(self):
        connected = super().connect()
        if connected:
            print("websocket connected:", self.url)
        return connected

    def send_json(self, payload):
        if not self.send_text(json.dumps(payload)):
            return False
        for _ in range(MAX_CONTROL_FLUSH_CHUNKS):
            if not self.tx_pending:
                break
            previous_offset = self._tx_offset
            if not self.flush_tx_chunk():
                return False
            if self._tx_offset == previous_offset:
                break
        return self.connected

    def recv_json(self):
        text = self.recv_text()
        if text is None:
            return None
        return json.loads(text)

    def recv_text(self):
        frame = self.recv_frame()
        if not frame:
            return None
        opcode, payload = frame
        if opcode != OP_TEXT:
            return None
        return payload.decode()
