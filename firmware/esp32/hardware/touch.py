class TouchInputs:
    def __init__(self):
        self._head = False
        self._back = False

    def poll(self):
        if self._head:
            self._head = False
            return "touch_head"
        if self._back:
            self._back = False
            return "touch_back"
        return None
