from media.protocol import FLAG_END_OF_UTTERANCE, FLAG_SPEECH


class EnergyVAD:
    def __init__(self, speech_threshold=500, end_silence_chunks=3):
        self.speech_threshold = max(1, speech_threshold)
        self.end_silence_chunks = max(1, end_silence_chunks)
        self.speaking = False
        self._silence_chunks = 0

    def reset(self):
        self.speaking = False
        self._silence_chunks = 0

    def process(self, pcm16):
        if len(pcm16) < 2:
            return 0
        total = 0
        samples = len(pcm16) // 2
        for index in range(0, samples * 2, 2):
            value = pcm16[index] | (pcm16[index + 1] << 8)
            if value >= 0x8000:
                value -= 0x10000
            total += abs(value)
        energy = total // samples
        if energy >= self.speech_threshold:
            self.speaking = True
            self._silence_chunks = 0
            return FLAG_SPEECH
        if not self.speaking:
            return 0
        self._silence_chunks += 1
        if self._silence_chunks < self.end_silence_chunks:
            return 0
        self.reset()
        return FLAG_END_OF_UTTERANCE
