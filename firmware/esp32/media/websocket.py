try:
    import usocket as socket
except ImportError:
    import socket

try:
    import ubinascii as binascii
except ImportError:
    import binascii

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib

try:
    import urandom as random
except ImportError:
    import random

try:
    import utime as time
except ImportError:
    import time

try:
    from config import (
        MEDIA_SOCKET_CONNECT_TIMEOUT_SEC,
        MEDIA_SOCKET_IO_TIMEOUT_SEC,
        MEDIA_SOCKET_SEND_CHUNK_BYTES,
    )
except ImportError:
    MEDIA_SOCKET_CONNECT_TIMEOUT_SEC = 0.01
    MEDIA_SOCKET_IO_TIMEOUT_SEC = 0.01
    MEDIA_SOCKET_SEND_CHUNK_BYTES = 1024

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CONTINUATION = 0x0
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_MAX_RX_BYTES = 256 * 1024 + 64
DEFAULT_MAX_MESSAGE_BYTES = 256 * 1024


class _ProtocolError(Exception):
    def __init__(self, close_code=1002):
        super().__init__("websocket protocol error")
        self.close_code = close_code


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


def _sha1(data):
    try:
        return hashlib.sha1(data).digest()
    except TypeError:
        digest = hashlib.sha1()
        digest.update(data)
        return digest.digest()


class MediaWebSocket:
    def __init__(
        self,
        url,
        io_timeout_sec=MEDIA_SOCKET_IO_TIMEOUT_SEC,
        connect_timeout_sec=MEDIA_SOCKET_CONNECT_TIMEOUT_SEC,
        send_chunk_bytes=MEDIA_SOCKET_SEND_CHUNK_BYTES,
        max_rx_bytes=DEFAULT_MAX_RX_BYTES,
        max_message_bytes=DEFAULT_MAX_MESSAGE_BYTES,
        headers=None,
    ):
        self.url = url
        self.io_timeout_sec = min(io_timeout_sec, 0.01)
        self.connect_timeout_sec = min(connect_timeout_sec, 0.01)
        self.send_chunk_bytes = max(1, min(send_chunk_bytes, 1024))
        self.max_rx_bytes = max(128, max_rx_bytes)
        self.max_message_bytes = max(1, max_message_bytes)
        self.headers = dict(headers or {})
        self.host, self.port, self.path = parse_ws_url(url)
        self.sock = None
        self.connected = False
        self._rx = bytearray()
        self._tx_header = None
        self._tx_payload = None
        self._tx_mask = None
        self._tx_offset = 0
        self._tx_total = 0
        self._tx_close_after = False
        self._control_queue = []
        self._fragment_opcode = None
        self._fragment_payload = bytearray()

    @property
    def tx_pending(self):
        return self._tx_header is not None

    def connect(self):
        self.close(send_close=False)
        try:
            address = socket.getaddrinfo(self.host, self.port)[0][-1]
            self.sock = socket.socket()
            if hasattr(self.sock, "settimeout"):
                self.sock.settimeout(self.connect_timeout_sec)
            self.sock.connect(address)
            self._handshake()
            if hasattr(self.sock, "settimeout"):
                self.sock.settimeout(self.io_timeout_sec)
            self.connected = True
            return True
        except Exception as exc:
            print("media websocket connect failed:", exc)
            self.close(send_close=False)
            return False

    def _handshake(self):
        key = _b64(_random_bytes(16))
        expected_accept = _b64(_sha1((key + GUID).encode()))
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            + f"Host: {self.host}:{self.port}\r\n"
            + "Upgrade: websocket\r\n"
            + "Connection: Upgrade\r\n"
            + f"Sec-WebSocket-Key: {key}\r\n"
            + "Sec-WebSocket-Version: 13\r\n"
        )
        for name, value in self.headers.items():
            name = str(name)
            value = str(value)
            if "\r" in name or "\n" in name or ":" in name:
                raise ValueError("invalid websocket header name")
            if "\r" in value or "\n" in value:
                raise ValueError("invalid websocket header value")
            request += f"{name}: {value}\r\n"
        request += "\r\n"
        self._send_http(request.encode())
        response = bytearray()
        boundary = -1
        while boundary < 0 and len(response) <= 4096:
            chunk = self.sock.recv(min(512, 4097 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
            boundary = response.find(b"\r\n\r\n")
        if boundary < 0:
            raise OSError("incomplete media websocket upgrade")
        header_end = boundary + 4
        header = bytes(response[:boundary])
        lines = header.split(b"\r\n")
        if not lines or b" 101 " not in lines[0]:
            raise OSError("media websocket upgrade rejected")
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                headers[name.strip().lower()] = value.strip()
        accept = headers.get(b"sec-websocket-accept", b"")
        if accept.decode() != expected_accept:
            raise OSError("invalid websocket accept")
        self._rx.extend(response[header_end:])

    def send_text(self, text):
        if not isinstance(text, bytes):
            text = text.encode()
        return self._queue_and_start(text, OP_TEXT)

    def send_binary(self, payload):
        return self._queue_and_start(payload, OP_BINARY)

    def _queue_and_start(self, payload, opcode):
        if not self.connected or self.sock is None or self.tx_pending:
            return False
        self._prepare_tx(payload, opcode)
        self._tx_offset = 0
        return self.flush_tx_chunk()

    def _prepare_tx(self, payload, opcode, close_after=False):
        payload = bytes(payload)
        length = len(payload)
        mask = _random_bytes(4)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.extend((0x80 | 126, (length >> 8) & 0xFF, length & 0xFF))
        else:
            header.append(0x80 | 127)
            for shift in range(56, -1, -8):
                header.append((length >> shift) & 0xFF)
        header.extend(mask)
        self._tx_header = bytes(header)
        self._tx_payload = payload
        self._tx_mask = mask
        self._tx_total = len(header) + length
        self._tx_close_after = close_after

    def flush_tx_chunk(self):
        if not self.tx_pending:
            return True
        if not self.connected or self.sock is None:
            return False
        chunk = self._tx_chunk()
        try:
            count = self.sock.send(chunk)
        except OSError as exc:
            if self._is_temporary_receive_error(exc):
                return True
            self.close(send_close=False)
            return False
        if not count:
            self.close(send_close=False)
            return False
        self._tx_offset += count
        if self._tx_offset >= self._tx_total:
            close_after = self._tx_close_after
            self._clear_tx()
            if close_after:
                self.close(send_close=False)
            else:
                self._promote_control()
        return True

    def _tx_chunk(self):
        remaining = min(self.send_chunk_bytes, self._tx_total - self._tx_offset)
        chunk = bytearray()
        header_length = len(self._tx_header)
        if self._tx_offset < header_length:
            header_end = min(header_length, self._tx_offset + remaining)
            chunk.extend(self._tx_header[self._tx_offset:header_end])
            remaining -= header_end - self._tx_offset
        if remaining:
            payload_start = max(0, self._tx_offset - header_length)
            payload_end = payload_start + remaining
            chunk.extend(
                self._tx_payload[index] ^ self._tx_mask[index % 4]
                for index in range(payload_start, payload_end)
            )
        return bytes(chunk)

    def _clear_tx(self):
        self._tx_header = None
        self._tx_payload = None
        self._tx_mask = None
        self._tx_offset = 0
        self._tx_total = 0
        self._tx_close_after = False

    def _promote_control(self):
        if self.tx_pending or not self._control_queue:
            return
        opcode, payload, close_after = self._control_queue.pop(0)
        self._prepare_tx(payload, opcode, close_after=close_after)

    def recv_frame(self):
        if not self.connected or self.sock is None:
            return None
        try:
            frame = self._parse_frame()
            if frame is None:
                remaining = self.max_rx_bytes - len(self._rx)
                if remaining <= 0:
                    raise _ProtocolError(1009)
                try:
                    chunk = self.sock.recv(min(4096, remaining))
                    if chunk:
                        self._rx.extend(chunk)
                    else:
                        self.close(send_close=False)
                        return None
                except OSError as exc:
                    if not self._is_temporary_receive_error(exc):
                        self.close(send_close=False)
                    return None
                frame = self._parse_frame()
            if frame is None:
                return None
            fin, opcode, payload = frame
            if opcode == OP_CLOSE:
                self._send_control_or_queue(
                    OP_CLOSE,
                    payload or b"\x03\xe8",
                    close_after=True,
                )
                return None
            if opcode == OP_PING:
                self._send_control_or_queue(OP_PONG, payload)
                return None
            if opcode == OP_PONG:
                return None
            return self._handle_data_frame(fin, opcode, payload)
        except _ProtocolError as exc:
            self._close_with_code(exc.close_code)
            return None
        except Exception as exc:
            print("media websocket recv failed:", exc)
            self.close(send_close=False)
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
        first, second = self._rx[0], self._rx[1]
        fin = bool(first & 0x80)
        rsv = first & 0x70
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        payload_length = second & 0x7F
        if rsv or masked:
            raise _ProtocolError(1002)
        if opcode not in (
            OP_CONTINUATION,
            OP_TEXT,
            OP_BINARY,
            OP_CLOSE,
            OP_PING,
            OP_PONG,
        ):
            raise _ProtocolError(1002)
        is_control = opcode >= 0x8
        if is_control and (not fin or payload_length >= 126):
            raise _ProtocolError(1002)
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
        if payload_length > self.max_rx_bytes - offset:
            raise _ProtocolError(1009)
        frame_end = offset + payload_length
        if len(self._rx) < frame_end:
            return None
        payload = bytes(self._rx[offset:frame_end])
        del self._rx[:frame_end]
        if opcode == OP_CLOSE and len(payload) == 1:
            raise _ProtocolError(1002)
        return fin, opcode, payload

    def _handle_data_frame(self, fin, opcode, payload):
        if opcode == OP_CONTINUATION:
            if self._fragment_opcode is None:
                raise _ProtocolError(1002)
            if len(self._fragment_payload) + len(payload) > self.max_message_bytes:
                raise _ProtocolError(1009)
            self._fragment_payload.extend(payload)
            if not fin:
                return None
            message_opcode = self._fragment_opcode
            message_payload = bytes(self._fragment_payload)
            self._clear_fragment()
            return message_opcode, message_payload
        if self._fragment_opcode is not None:
            raise _ProtocolError(1002)
        if len(payload) > self.max_message_bytes:
            raise _ProtocolError(1009)
        if fin:
            return opcode, payload
        self._fragment_opcode = opcode
        self._fragment_payload = bytearray(payload)
        return None

    def _clear_fragment(self):
        self._fragment_opcode = None
        self._fragment_payload = bytearray()

    def _encode_frame(self, payload, opcode=OP_TEXT):
        payload = bytes(payload)
        length = len(payload)
        if opcode >= 0x8 and length > 125:
            raise ValueError("websocket control frame too large")
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

    def _send_http(self, data):
        offset = 0
        while offset < len(data):
            end = min(len(data), offset + self.send_chunk_bytes)
            count = self.sock.send(data[offset:end])
            if not count:
                raise OSError("media socket send failed")
            offset += count

    def _send_control_now(self, opcode, payload=b""):
        if self.sock is None:
            return False
        try:
            frame = self._encode_frame(payload, opcode)
            offset = 0
            while offset < len(frame):
                count = self.sock.send(frame[offset : offset + self.send_chunk_bytes])
                if not count:
                    return False
                offset += count
            return True
        except Exception:
            return False

    def _send_control_or_queue(self, opcode, payload=b"", close_after=False):
        if self.tx_pending:
            self._control_queue.append((opcode, bytes(payload), close_after))
            return True
        sent = self._send_control_now(opcode, payload)
        if close_after:
            self.close(send_close=False)
        return sent

    def _close_with_code(self, code):
        self._send_control_or_queue(
            OP_CLOSE,
            bytes(((code >> 8) & 0xFF, code & 0xFF)),
            close_after=True,
        )

    def close(self, send_close=True):
        if send_close and self.connected:
            if self.tx_pending:
                self._control_queue.append((OP_CLOSE, b"\x03\xe8", True))
                return
            self._send_control_now(OP_CLOSE, b"\x03\xe8")
        self.connected = False
        self._rx = bytearray()
        self._clear_tx()
        self._control_queue = []
        self._clear_fragment()
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
