TONE_PATTERNS = {
    "soft": ((1600, 80),),
    "happy": ((1800, 70), (2200, 90)),
    "alert": ((2600, 100), (0, 60), (2600, 100)),
    "low_battery": ((900, 180), (0, 90), (900, 180)),
}


class Buzzer:
    """仅转发非阻塞 tone 请求；真实 MAX98357A 播放由媒体 worker 接管。"""

    def __init__(self, request_tone=None):
        self.request_tone = request_tone

    def _request(self, payload):
        if self.request_tone:
            try:
                self.request_tone(payload)
                return True
            except Exception as exc:
                print("tone request failed:", exc)
                return False
        print("tone request:", payload)
        return True

    def play_step(self, freq, duration_ms):
        return self._request({"frequency_hz": int(freq), "duration_ms": int(duration_ms)})

    def off(self):
        return self._request({"stop": True})

    def chirp(self, tone="soft"):
        if tone not in TONE_PATTERNS:
            tone = "soft"
        return self._request(tone)
