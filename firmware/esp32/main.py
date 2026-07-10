try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

try:
    import utime as time
except ImportError:
    import time

from comm.websocket_client import WebSocketClient
from comm.wifi_config import connect_wifi
from config import (
    AGENT_WS_URL,
    HEARTBEAT_INTERVAL_MS,
    SESSION_ID,
    WIFI_CONNECT_TIMEOUT_MS,
    WIFI_PASSWORD,
    WIFI_SSID,
    WS_RECONNECT_DELAY_MS,
)
from control.motion_controller import MotionController
from hardware.buzzer import Buzzer
from hardware.display import Display
from hardware.imu import IMU
from hardware.power import PowerMonitor
from hardware.servo import ServoBank
from hardware.touch import TouchInputs
from interaction.event_adapter import EventAdapter
from interaction.local_fallback import LocalFallback
from motion.action_player import ActionPlayer
from reflex.reflex_controller import ReflexController
from safety.guard import SafetyGuard


def now_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def ticks_diff(end, start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(end, start)
    return end - start


async def sleep_ms(ms):
    if hasattr(asyncio, "sleep_ms"):
        await asyncio.sleep_ms(ms)
    else:
        await asyncio.sleep(ms / 1000)


class FirmwareProtocol:
    def __init__(self, session_id):
        self.session_id = session_id
        self.seq = 0

    def envelope(self, msg_type, payload=None, priority=3, msg_id=None):
        self.seq += 1
        if not msg_id:
            msg_id = f"{msg_type}_{now_ms()}_{self.seq}"
        return {
            "type": msg_type,
            "id": msg_id,
            "seq": self.seq,
            "ts_ms": now_ms(),
            "session_id": self.session_id,
            "priority": priority,
            "payload": payload or {},
        }

    def robot_payload(self, power, imu, reflex=None, motion=None):
        payload = {"battery_pct": power.battery_pct, "posture": imu.read_posture()}
        if reflex:
            motion_state = motion.motion_state if motion else "idle"
            payload.update(reflex.status(motion_state))
        return payload

    def event(self, name, power, imu, reflex=None, motion=None):
        payload = self.robot_payload(power, imu, reflex, motion)
        payload.update({"name": name, "source": "esp32"})
        if name == "fall_detected" and reflex:
            payload["reflex_taken"] = reflex.last_reflex
            payload["recovered"] = reflex.state == "recovered"
        return self.envelope("event", payload)

    def ack(self, intent_id, status="accepted"):
        return self.envelope("ack", {"intent_id": intent_id, "status": status})

    def error(self, code, message, intent_id=None):
        payload = {"code": code, "message": message}
        if intent_id:
            payload["intent_id"] = intent_id
        return self.envelope("error", payload, priority=8)

    def status(self, power, imu, reflex=None, motion=None):
        return self.envelope("status", self.robot_payload(power, imu, reflex, motion))

    def heartbeat(self, power, imu, reflex=None, motion=None):
        return self.envelope(
            "heartbeat",
            self.robot_payload(power, imu, reflex, motion),
            priority=1,
        )


def _semantic_actions(payload):
    action_items = []
    if payload.get("expression"):
        action_items.append({"name": "set_expression", "args": payload.get("expression")})
    skills = payload.get("skills", [])
    actions = payload.get("actions", [])
    if skills is None:
        skills = []
    if actions is None:
        actions = []
    if not isinstance(skills, list) or not isinstance(actions, list):
        return None
    for skill in skills:
        if not isinstance(skill, dict):
            return None
        action_items.append({"name": skill.get("name"), "args": skill.get("args", {})})
    action_items.extend(actions)
    return action_items


def execute_intent(message, actions, safety, protocol, ws, motion=None, reflex=None):
    intent_id = message.get("id")
    payload = message.get("payload", {})
    action_list = _semantic_actions(payload)
    if not isinstance(action_list, list):
        ws.send_json(protocol.error("INVALID_INTENT", "payload actions/skills must be a list", intent_id))
        return

    if any(isinstance(action, dict) and action.get("name") == "stop" for action in action_list):
        if reflex:
            reflex.request_emergency_stop()
        if motion:
            motion.cancel("stop")
        else:
            actions.stop()
        ws.send_json(protocol.ack(intent_id))
        return

    for action in action_list:
        if not isinstance(action, dict) or not safety.can_execute(action):
            ws.send_json(protocol.error("POLICY_DENIED", "firmware rejected unsafe action", intent_id))
            return

    if motion:
        accepted, reason = motion.submit_intent(message)
        if not accepted:
            ws.send_json(protocol.error("EXECUTION_FAILED", reason, intent_id))
            return
    else:
        for action in action_list:
            result = actions.execute(action)
            if not result.accepted:
                ws.send_json(protocol.error("EXECUTION_FAILED", result.reason, intent_id))
                return
    ws.send_json(protocol.ack(intent_id))


def handle_agent_message(message, actions, safety, protocol, ws, motion=None, reflex=None):
    if not isinstance(message, dict):
        return
    msg_type = message.get("type")
    if msg_type == "intent":
        execute_intent(message, actions, safety, protocol, ws, motion, reflex)


async def network_loop(display, power, imu, actions, safety, protocol, state, motion, reflex):
    while True:
        if not connect_wifi(WIFI_SSID, WIFI_PASSWORD, WIFI_CONNECT_TIMEOUT_MS):
            display.show_status("wifi offline")
            await sleep_ms(WS_RECONNECT_DELAY_MS)
            continue

        ws = WebSocketClient(AGENT_WS_URL)
        if not ws.connect():
            display.show_status("agent offline")
            await sleep_ms(WS_RECONNECT_DELAY_MS)
            continue

        state["ws"] = ws
        display.show_status("agent online")
        ws.send_json(protocol.status(power, imu, reflex, motion))
        last_heartbeat = now_ms()

        while ws.connected:
            try:
                message = ws.recv_json()
                if message:
                    handle_agent_message(message, actions, safety, protocol, ws, motion, reflex)
                if ticks_diff(now_ms(), last_heartbeat) >= HEARTBEAT_INTERVAL_MS:
                    ws.send_json(protocol.heartbeat(power, imu, reflex, motion))
                    last_heartbeat = now_ms()
            except Exception as exc:
                print("network loop error:", exc)
                ws.close()
            await sleep_ms(100)

        state["ws"] = None
        display.show_status("agent offline")
        await sleep_ms(WS_RECONNECT_DELAY_MS)


async def main():
    display = Display()
    imu = IMU()
    power = PowerMonitor()
    touch = TouchInputs()
    servos = ServoBank()
    buzzer = Buzzer()
    actions = ActionPlayer(servos, display, buzzer)
    safety = SafetyGuard(power, imu)
    reflex = ReflexController(power, imu, actions, display, buzzer)
    motion = MotionController(actions, safety, reflex, now_ms)
    fallback = LocalFallback(display, actions)
    adapter = EventAdapter(touch, imu, power)
    protocol = FirmwareProtocol(SESSION_ID)
    network_state = {"ws": None}

    display.set_face("idle")
    print("naobot firmware booted; agent:", AGENT_WS_URL)
    asyncio.create_task(network_loop(display, power, imu, actions, safety, protocol, network_state, motion, reflex))

    while True:
        if reflex.check():
            motion.cancel("reflex")
            reflex.run()
        motion.tick()
        event = adapter.poll()
        if event:
            if not safety.can_emit_event(event):
                actions.stop()
                display.set_face("alert")
            else:
                ws = network_state.get("ws")
                envelope = protocol.event(event, power, imu, reflex, motion)
                if not ws or not ws.connected or not ws.send_json(envelope):
                    fallback.handle(event)
        await sleep_ms(50)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print("fatal:", exc)
