class IMU:
    def __init__(self):
        self.posture = "upright"

    def read_posture(self):
        return self.posture

    def is_fault(self):
        return self.posture not in ("upright", "sitting")
