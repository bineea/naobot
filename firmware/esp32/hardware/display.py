class Display:
    def __init__(self):
        self.face = "idle"

    def set_face(self, face):
        self.face = face
        print("face:", face)

    def blink(self):
        self.set_face("idle")
