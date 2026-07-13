try:
    import usocket as socket
except ImportError:
    import socket

try:
    import ubinascii as binascii
except ImportError:
    import binascii

try:
    import urandom as random
except ImportError:
    import random

try:
    import utime as time
except ImportError:
    import time

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def parse_ws_url(url):
    if not url.startswith("ws://"):
        raise ValueError("only ws:// is supported")
    rest = url[5:]
    slash = rest.find("/")
    host_port = rest if slash == -1 else rest[:slash]
    path = "/" if slash == -1 else rest[slash:] or "/"
    if ":" in host_port:
        host, raw_port = host_port.rsplit(":", 1)
        port = int(raw_port)
    else:
        host, port = host_port, 80
    if not host:
        raise ValueError("missing websocket host")
    return host, port, path


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


def _b64(data):
    encoded = binascii.b2a_base64(data)
    if isinstance(encoded, bytes):
        encoded = encoded.decode()
    return encoded.strip()


class MediaWebSocket:
    def __init__(self, url, io_timeout_sec=0.02):
        self.url = url
        self.io_timeout_sec = io_timeout_sec
        self.host, self.port, self.path = parse_ws_url(url)
        self.sock = None
        self.connected = False
        self._rx = bytearray()

    def connect(self):
        self.close()
        try:
            address = socket.getaddrinfo(self.host, self.port)[0][-1]
            self.sock = socket.socket()
            if hasattr(self.sock, "settimeout"):
                self.sock.settimeout(2)
            self.sock.connect(address)
            self._handshake()
            if hasattr(self.sock, "settimeout"):
                self.sock.settimeout(self.io_timeout_sec)
            self.connected = True
            return True
        except Exception as exc:
            print("media websocket connect failed:", exc)
            self.close()
            return False

    def _handshake(self):
        key = _b64(_random_bytes(16))
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            + f"Host: {self.host}:{self.port}\r\n"
            + "Upgrade: websocket\r\n"
            + "Connection: Upgrade\r\n"
            + f"Sec-WebSocket-Key: {key}\r\n"
            + "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._send_all(request.encode())
        response = b""
        while b"\r\n\r\n" not in response and len(response) <= 4096:
            chunk = self.sock.recv(512)
            if not chunk:
                break
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise OSError("media websocket upgrade rejected")

    def send_text(self, text):
        if not isinstance(text, bytes):
            text = text.encode()
        return self._send_frame(text, OP_TEXT)

    def send_binary(self, payload):
        return self._send_frame(payload, OP_BINARY)

    def _send_frame(self, payload, opcode):
        if not self.connected or self.sock is None:
            return False
        try:
            self._send_all(self._encode_frame(payload, opcode))
            return True
        except Exception:
            self.close()
            return False

    def recv_frame(self):
        if not self.connected or self.sock is None:
            return None
        try:
            chunk = self.sock.recv(4096)
            if chunk:
                self._rx.extend(chunk)
            else:
                self.close()
                return None
        except OSError as exc:
            if not self._is_temporary_receive_error(exc):
                self.close()
                return None
        frame = self._parse_frame()
        if frame is None:
            return None
        opcode, payload = frame
        if opcode == OP_CLOSE:
            self.close()
            return None
        if opcode == OP_PING:
            self._send_frame(payload, OP_PONG)
            return None
        if opcode in (OP_TEXT, OP_BINARY):
            return opcode, payload
        return None

    @staticmethod
    def _is_temporary_receive_error(exc):
        code = exc.args[0] if exc.args else None
        if code in (11, 110, 116, 10035, 10060):
            return True
        return exc.__class__.__name__ in ("BlockingIOError", "TimeoutError")

    def _parse_frame(self):
        if len(self._rx) < 2:
            return None
        opcode = self._rx[0] & 0x0F
        masked = bool(self._rx[1] & 0x80)
        payload_length = self._rx[1] & 0x7F
        offset = 2
        if payload_length == 126:
            if len(self._rx) < 4:
                return None
            payload_length = (self._rx[2] << 8) | self._rx[3]
            offset = 4
        elif payload_length == 127:
            if len(self._rx) < 10:
                return None
            payload_length = 0
            for value in self._rx[2:10]:
                payload_length = (payload_length << 8) | value
            offset = 10
        mask = None
        if masked:
            if len(self._rx) < offset + 4:
                return None
            mask = self._rx[offset : offset + 4]
            offset += 4
        frame_end = offset + payload_length
        if len(self._rx) < frame_end:
            return None
        payload = bytes(self._rx[offset:frame_end])
        del self._rx[:frame_end]
        if mask:
            payload = bytes(payload[index] ^ mask[index % 4] for index in range(payload_length))
        return opcode, payload

    def _encode_frame(self, payload, opcode=OP_TEXT):
        payload = bytes(payload)
        length = len(payload)
        mask = _random_bytes(4)
        frame = bytearray([0x80 | opcode])
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.extend((0x80 | 126, (length >> 8) & 0xFF, length & 0xFF))
        else:
            frame.append(0x80 | 127)
            for shift in range(56, -1, -8):
                frame.append((length >> shift) & 0xFF)
        frame.extend(mask)
        frame.extend(bytes(payload[index] ^ mask[index % 4] for index in range(length)))
        return bytes(frame)

    def _send_all(self, data):
        if hasattr(self.sock, "sendall"):
            self.sock.sendall(data)
            return
        sent = 0
        while sent < len(data):
            count = self.sock.send(data[sent:])
            if not count:
                raise OSError("media socket send failed")
            sent += count

    def close(self):
        self.connected = False
        self._rx = bytearray()
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
