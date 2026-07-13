class MotionController:
    def __init__(self, actions, safety, reflex, now_ms):
        self.actions = actions
        self.safety = safety
        self.reflex = reflex
        self.now_ms = now_ms
        self.current = None
        self.queue = []
        self.motion_state = "idle"

    def submit_action(self, action):
        name = action.get("name")
        if name == "stop":
            self.cancel("stop")
            return True, ""
        if not self.safety.can_execute(action):
            return False, "firmware rejected unsafe action"
        skill = self.actions.build_skill(name, action.get("args", {}))
        self.queue.append(skill)
        if not self.current:
            self._start_next()
        return True, ""

    def submit_intent(self, message):
        payload = message.get("payload", {})
        expression = payload.get("expression")
        if expression:
            accepted, reason = self.submit_action({"name": "set_expression", "args": expression})
            if not accepted:
                return False, reason
        action_items = []
        skills = payload.get("skills") or []
        for skill in skills:
            action_items.append({"name": skill.get("name"), "args": skill.get("args", {})})
        if not expression and not skills:
            action_items.extend(payload.get("actions") or [])
        for action in action_items:
            accepted, reason = self.submit_action(action)
            if not accepted:
                return False, reason
        return True, ""

    def tick(self):
        if self.reflex and self.reflex.check():
            self.cancel("reflex")
            return
        if not self.current:
            self._start_next()
            return
        if self.current.tick(self.now_ms()):
            self.current = None
            self._start_next()

    def cancel(self, reason="cancelled"):
        if self.current:
            self.current.cancel()
            self.current = None
        self.queue = []
        self.actions.stop()
        self.motion_state = "stopped" if reason == "stop" else "cancelled"

    def is_running(self):
        return self.current is not None

    def _start_next(self):
        if not self.queue:
            self.motion_state = "idle"
            return
        self.current = self.queue.pop(0)
        self.motion_state = self.current.name
        self.current.start(self.now_ms())
