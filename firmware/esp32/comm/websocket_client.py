class WebSocketClient:
    """占位 WebSocket client。

    MicroPython 上的 WebSocket 库选型需按固件版本确认。当前骨架保留
    接口，MVP 的 CPython 模拟器先覆盖协议联调。
    """

    def __init__(self, url):
        self.url = url
        self.connected = False

    def connect(self):
        print("connect websocket:", self.url)
        self.connected = False

    def send(self, envelope):
        print("send envelope:", envelope)
