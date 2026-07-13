import json

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

try:
    from config import WS_CONNECT_TIMEOUT_SEC, WS_SOCKET_TIMEOUT_SEC
except ImportError:
    WS_CONNECT_TIMEOUT_SEC = 2
    WS_SOCKET_TIMEOUT_SEC = 1


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def parse_ws_url(url):
    if not url.startswith("ws://"):
        raise ValueError("only ws:// is supported")
    rest = url[5:]
    slash = rest.find("/")
    if slash == -1:
        host_port = rest
        path = "/"
    else:
        host_port = rest[:slash]
        path = rest[slash:] or "/"

    if ":" in host_port:
        host, port_text = host_port.rsplit(":", 1)
        port = int(port_text)
    else:
        host = host_port
        port = 80
    if not host:
        raise ValueError("missing websocket host")
    return host, port, path


def _b64(data):
    encoded = binascii.b2a_base64(data)
    if isinstance(encoded, bytes):
        encoded = encoded.decode()
    return encoded.strip()


def _random_bytes(length):
    try:
        return random.urandom(length)
    except AttributeError:
        pass
    try:
        return bytes(random.getrandbits(8) for _ in range(length))
    except AttributeError:
        seed = int(time.time() * 1000)
        return bytes((seed + i * 37) & 0xFF for i in range(length))


def _read_exact(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise OSError("socket closed")
        data += chunk
    return data


class WebSocketClient:
    def __init__(self, url):
        self.url = url
        self.connected = False
        self.sock = None
        self.host = ""
        self.port = 0
        self.path = "/"

    def connect(self):
        self.close()
        try:
            self.host, self.port, self.path = parse_ws_url(self.url)
            addr = socket.getaddrinfo(self.host, self.port)[0][-1]
            self.sock = socket.socket()
            if hasattr(self.sock, "settimeout"):
                self.sock.settimeout(WS_CONNECT_TIMEOUT_SEC)
            self.sock.connect(addr)
            self._handshake()
            if hasattr(self.sock, "settimeout"):
                self.sock.settimeout(WS_SOCKET_TIMEOUT_SEC)
            self.connected = True
            print("websocket connected:", self.url)
            return True
        except Exception as exc:
            print("websocket connect failed:", exc)
            self.close()
            return False

    def _handshake(self):
        key = _b64(_random_bytes(16))
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._send_all(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(256)
            if not chunk:
                break
            response += chunk
            if len(response) > 2048:
                break
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise OSError("websocket upgrade rejected")

    def send_json(self, payload):
        return self.send_text(json.dumps(payload))

    def send_text(self, text):
        if not self.connected or not self.sock:
            return False
        if not isinstance(text, bytes):
            text = text.encode()
        try:
            self._send_all(self._encode_frame(text))
            return True
        except Exception as exc:
            print("websocket send failed:", exc)
            self.close()
            return False

    def recv_json(self):
        text = self.recv_text()
        if not text:
            return None
        return json.loads(text)

    def recv_text(self):
        if not self.connected or not self.sock:
            return None
        try:
            header = self.sock.recv(2)
            if not header:
                self.close()
                return None
            if len(header) < 2:
                header += _read_exact(self.sock, 2 - len(header))
            opcode = header[0] & 0x0F
            masked = header[1] & 0x80
            length = header[1] & 0x7F
            if length == 126:
                ext = _read_exact(self.sock, 2)
                length = (ext[0] << 8) | ext[1]
            elif length == 127:
                raise OSError("large websocket frame unsupported")

            mask = _read_exact(self.sock, 4) if masked else None
            payload = _read_exact(self.sock, length) if length else b""
            if mask:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(length))

            if opcode == 0x8:
                self.close()
                return None
            if opcode == 0x9:
                self._send_control(0xA, payload)
                return None
            if opcode != 0x1:
                return None
            return payload.decode()
        except OSError:
            return None
        except Exception as exc:
            print("websocket recv failed:", exc)
            self.close()
            return None

    def _send_control(self, opcode, payload=b""):
        if self.sock:
            self._send_all(self._encode_frame(payload, opcode=opcode))

    def _encode_frame(self, payload, opcode=0x1):
        length = len(payload)
        mask = _random_bytes(4)
        frame = bytearray()
        frame.append(0x80 | opcode)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(bytes([(length >> 8) & 0xFF, length & 0xFF]))
        else:
            raise ValueError("websocket frame too large")
        frame.extend(mask)
        frame.extend(bytes(payload[i] ^ mask[i % 4] for i in range(length)))
        return bytes(frame)

    def _send_all(self, data):
        if hasattr(self.sock, "sendall"):
            self.sock.sendall(data)
            return
        sent = 0
        while sent < len(data):
            count = self.sock.send(data[sent:])
            if count is None:
                return
            if count == 0:
                raise OSError("socket send failed")
            sent += count

    def close(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
