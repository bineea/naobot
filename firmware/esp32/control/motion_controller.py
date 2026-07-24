class MotionController:
    """动作队列调度器。按 tick 驱动 skill，支持有界队列、intent_id 去重和
    skill 自然完成/中断的终态回调（on_intent_done），供上层回执 ack completed/failed。"""

    def __init__(
        self,
        actions,
        safety,
        reflex,
        now_ms,
        *,
        queue_capacity=8,
        seen_capacity=32,
        on_intent_done=None,
    ):
        self.actions = actions
        self.safety = safety
        self.reflex = reflex
        self.now_ms = now_ms
        self.current = None
        self.queue = []
        self.motion_state = "idle"
        self.queue_capacity = queue_capacity
        self.seen_capacity = seen_capacity
        self.on_intent_done = on_intent_done
        self._seen = []  # intent_id LRU，防重放
        self._pending = {}  # intent_id -> 待完成 skill 计数
        self._current_intent_id = None  # submit_intent 进行中的 intent_id
        self.motion_inhibited = False
        self.motion_inhibit_reason = None

    def submit_action(self, action):
        name = action.get("name")
        if name == "stop":
            self.cancel("stop")
            return True, ""
        if self.motion_inhibited:
            return False, "motion inhibited: " + (self.motion_inhibit_reason or "safety")
        if not self.safety.can_execute(action):
            return False, "firmware rejected unsafe action"
        if len(self.queue) + (1 if self.current else 0) >= self.queue_capacity:
            return False, "motion queue full"
        skill = self.actions.build_skill(name, action.get("args", {}))
        skill.intent_id = self._current_intent_id
        if self._current_intent_id is not None:
            self._pending[self._current_intent_id] = self._pending.get(self._current_intent_id, 0) + 1
        self.queue.append(skill)
        if not self.current:
            self._start_next()
        return True, ""

    def submit_intent(self, message):
        intent_id = message.get("id")
        if intent_id is not None and intent_id in self._seen:
            return True, "duplicate"
        if intent_id is not None:
            self._remember_seen(intent_id)
            self._pending[intent_id] = 0
        self._current_intent_id = intent_id
        try:
            payload = message.get("payload", {})
            expression = payload.get("expression")
            if expression:
                accepted, reason = self.submit_action({"name": "set_expression", "args": expression})
                if not accepted:
                    self._abort_intent(intent_id)
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
                    self._abort_intent(intent_id)
                    return False, reason
        finally:
            self._current_intent_id = None
        return True, ""

    def tick(self):
        if self.motion_inhibited:
            if self.current is not None or self.queue:
                self.cancel(self.motion_inhibit_reason or "safety")
            else:
                self.actions.stop()
                self.motion_state = "inhibited"
            return
        if self.reflex and self.reflex.check():
            self.cancel("reflex")
            return
        if not self.current:
            self._start_next()
            return
        if self.current.tick(self.now_ms()):
            intent_id = getattr(self.current, "intent_id", None)
            self.current = None
            if intent_id is not None:
                self._decrement_pending(intent_id)
            self._start_next()

    def cancel(self, reason="cancelled"):
        if self.current:
            self.current.cancel()
            self.current = None
        self.queue = []
        self.actions.stop()
        if self.motion_inhibited:
            self.motion_state = "inhibited"
        else:
            self.motion_state = "stopped" if reason == "stop" else "cancelled"
        if self.on_intent_done:
            for intent_id in list(self._pending.keys()):
                self.on_intent_done(intent_id, "failed", reason)
        self._pending = {}

    def set_motion_inhibited(self, inhibited, reason="safety"):
        inhibited = bool(inhibited)
        if inhibited:
            self.motion_inhibited = True
            self.motion_inhibit_reason = reason or "safety"
            self.cancel(self.motion_inhibit_reason)
            self.motion_state = "inhibited"
            return True
        if not self.motion_inhibited:
            return True
        if reason and reason != self.motion_inhibit_reason:
            return False
        self.motion_inhibited = False
        self.motion_inhibit_reason = None
        if self.current is None and not self.queue:
            self.motion_state = "idle"
        return True

    def is_running(self):
        return self.current is not None

    def _start_next(self):
        if self.motion_inhibited:
            self.actions.stop()
            self.motion_state = "inhibited"
            return
        if not self.queue:
            self.motion_state = "idle"
            return
        self.current = self.queue.pop(0)
        self.motion_state = self.current.name
        self.current.start(self.now_ms())

    def _decrement_pending(self, intent_id):
        remaining = self._pending.get(intent_id, 0) - 1
        if remaining <= 0:
            self._pending.pop(intent_id, None)
            if self.on_intent_done:
                self.on_intent_done(intent_id, "completed")
        else:
            self._pending[intent_id] = remaining

    def _abort_intent(self, intent_id):
        """submit_intent 部分入队失败时清理该 intent 的已入队 skill，caller 走 error 回执。"""
        if intent_id is None:
            return
        self.queue = [s for s in self.queue if getattr(s, "intent_id", None) != intent_id]
        if self.current is not None and getattr(self.current, "intent_id", None) == intent_id:
            self.current.cancel()
            self.current = None
        self._pending.pop(intent_id, None)

    def _remember_seen(self, intent_id):
        if intent_id in self._seen:
            self._seen.remove(intent_id)
        self._seen.append(intent_id)
        if len(self._seen) > self.seen_capacity:
            del self._seen[0]
