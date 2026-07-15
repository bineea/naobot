try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

try:
    import utime as time
except ImportError:
    import time

from comm.connection_worker import ConnectionWorker
from comm.websocket_client import WebSocketClient
from comm.wifi_config import connect_wifi_async
from config import (
    AGENT_WS_URL,
    BRAIN_HEARTBEAT_TIMEOUT_MS,
    DEVICE_TOKEN,
    FIRMWARE_HEARTBEAT_INTERVAL_MS,
    MEDIA_EVENT_BOOST_MS,
    SAFETY_LOOP_PERIOD_MS,
    SESSION_ID,
    WIFI_CONNECT_TIMEOUT_MS,
    WIFI_PASSWORD,
    WIFI_SSID,
    WS_RECONNECT_DELAY_MS,
)
from control.motion_controller import MotionController
from hardware.buzzer import Buzzer
from hardware.display import Display
from hardware.i2c import SharedI2C
from hardware.imu import IMU
from hardware.power import PowerMonitor
from hardware.servo import ServoBank
from hardware.touch import TouchInputs
from interaction.event_adapter import EventAdapter
from interaction.local_fallback import LocalFallback
from media.runtime_worker import create_default_worker
from motion.action_player import ActionPlayer
from reflex.reflex_controller import ReflexController
from safety.guard import MOVEMENT_ACTIONS, SafetyGuard


def now_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def ticks_diff(end, start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(end, start)
    return end - start


def ticks_add(start, delta):
    if hasattr(time, "ticks_add"):
        return time.ticks_add(start, delta)
    return start + delta


async def sleep_ms(ms):
    if hasattr(asyncio, "sleep_ms"):
        await asyncio.sleep_ms(ms)
    else:
        await asyncio.sleep(ms / 1000)


async def sleep_to_safety_deadline(
    loop_start_ms,
    state,
    clock=now_ms,
    sleeper=sleep_ms,
    period_ms=SAFETY_LOOP_PERIOD_MS,
):
    work_ms = max(0, ticks_diff(clock(), loop_start_ms))
    delay_ms = max(0, period_ms - work_ms)
    state["local_loop_ms"] = work_ms
    state["local_loop_overrun_ms"] = max(0, work_ms - period_ms)
    if delay_ms:
        await sleeper(delay_ms)
    return delay_ms


async def wait_for_connection(worker, poll_interval_ms=5):
    worker.start()
    while True:
        done, transport = worker.poll()
        if done:
            return transport
        await sleep_ms(poll_interval_ms)


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

    def robot_payload(self, power, imu, reflex=None, motion=None, state=None):
        state = state or {}
        media_state = state.get("media", state)
        payload = {
            "source": "firmware",
            "uptime_ms": now_ms(),
            "battery_pct": power.battery_pct,
            "voltage_mv": getattr(power, "voltage_mv", None),
            "current_ma": getattr(power, "current_ma", None),
            "charging": getattr(power, "charging", None),
            "external_power": getattr(power, "external_power", None),
            "power_fault": getattr(power, "fault", "unknown"),
            "power_available": getattr(power, "available", False),
            "power_level": getattr(power, "level", "unknown"),
            "posture": getattr(imu, "posture", "unknown"),
            "servo_output_enabled": state.get("servo_output_enabled", False),
            "local_loop_ms": state.get("local_loop_ms", 0),
            "local_loop_interval_ms": state.get("local_loop_interval_ms", 0),
            "local_loop_overrun_ms": state.get("local_loop_overrun_ms", 0),
            "agent_online": state.get("agent_online", False),
            "camera_fps": media_state.get("camera_fps", 0),
            "audio_state": media_state.get("audio_state", "unavailable"),
            "media_queue": media_state.get("media_queue", 0),
            "media_dropped": media_state.get("media_dropped", 0),
            "psram_free": media_state.get("psram_free", 0),
        }
        if reflex:
            motion_state = motion.motion_state if motion else "idle"
            payload.update(reflex.status(motion_state))
        return payload

    def event(self, name, power, imu, reflex=None, motion=None, state=None):
        payload = self.robot_payload(power, imu, reflex, motion, state)
        payload.update({"name": name, "source": "esp32"})
        if name == "fall_detected" and reflex:
            payload["reflex_taken"] = reflex.last_reflex
            payload["recovered"] = reflex.state == "recovered"
        return self.envelope("event", payload)

    def ack(self, intent_id, status="accepted", reason=None):
        payload = {"intent_id": intent_id, "status": status}
        if reason:
            payload["reason"] = reason
        return self.envelope("ack", payload)

    def error(self, code, message, intent_id=None):
        payload = {"code": code, "message": message}
        if intent_id:
            payload["intent_id"] = intent_id
        return self.envelope("error", payload, priority=8)

    def status(self, power, imu, reflex=None, motion=None, state=None):
        return self.envelope("status", self.robot_payload(power, imu, reflex, motion, state))

    def heartbeat(self, power, imu, reflex=None, motion=None, state=None):
        return self.envelope(
            "heartbeat",
            self.robot_payload(power, imu, reflex, motion, state),
            priority=1,
        )


def _semantic_actions(payload):
    action_items = []
    expression = payload.get("expression")
    if expression:
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
    if not expression and not skills:
        action_items.extend(actions)
    return action_items


def _estimated_host_now(state, clock):
    if not state:
        return None
    host_ts = state.get("last_host_ts_ms")
    local_seen = state.get("last_host_clock_seen_ms")
    if not isinstance(host_ts, int) or not isinstance(local_seen, int):
        return None
    age = ticks_diff(clock(), local_seen)
    if age < 0 or age > BRAIN_HEARTBEAT_TIMEOUT_MS:
        return None
    return host_ts + age


def _has_movement(action_list):
    return any(
        isinstance(action, dict) and action.get("name") in MOVEMENT_ACTIONS
        for action in action_list
    )


def execute_intent(
    message,
    actions,
    safety,
    protocol,
    ws,
    motion=None,
    reflex=None,
    state=None,
    clock=now_ms,
):
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

    if hasattr(safety, "can_accept_payload") and not safety.can_accept_payload(payload):
        ws.send_json(protocol.error("POLICY_DENIED", "intent contains forbidden fields", intent_id))
        return

    if reflex and hasattr(reflex, "check"):
        reflex.check()
    reflex_state = getattr(reflex, "state", "none") if reflex else "none"
    if getattr(reflex, "emergency_stop", False) or reflex_state in (
        "emergency_stop",
        "fall_detected",
        "recovering",
        "fault",
    ):
        ws.send_json(protocol.error("REFLEX_ACTIVE", "active reflex rejected intent", intent_id))
        return
    if reflex_state == "low_battery" and _has_movement(action_list):
        ws.send_json(protocol.error("REFLEX_ACTIVE", "low battery rejected movement", intent_id))
        return

    host_now = _estimated_host_now(state, clock)
    if _has_movement(action_list) and host_now is None:
        ws.send_json(
            protocol.error(
                "HOST_CLOCK_UNAVAILABLE",
                "movement requires a recent host heartbeat clock",
                intent_id,
            )
        )
        return
    deadline_ms = message.get("deadline_ms")
    if deadline_ms is not None and host_now is not None:
        intent_ts = message.get("ts_ms")
        if (
            not isinstance(intent_ts, int)
            or not isinstance(deadline_ms, int)
            or deadline_ms < 0
            or host_now > intent_ts + deadline_ms
        ):
            ws.send_json(protocol.error("INTENT_EXPIRED", "intent deadline expired", intent_id))
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
        # accepted=True（含 duplicate 重复确认）：发 accepted，skill 自然完成/中断时
        # 由 MotionController.on_intent_done 回调发 completed/failed。
        ws.send_json(protocol.ack(intent_id, "accepted"))
        return
    # 非 motion 路径：同步执行，成功即终态 completed。
    for action in action_list:
        result = actions.execute(action)
        if not result.accepted:
            ws.send_json(protocol.error("EXECUTION_FAILED", result.reason, intent_id))
            return
    ws.send_json(protocol.ack(intent_id, "completed"))


def handle_agent_message(message, actions, safety, protocol, ws, motion=None, reflex=None, state=None, display=None):
    if not isinstance(message, dict):
        return
    if state is not None:
        state["last_brain_seen_ms"] = now_ms()
        if not state.get("agent_online"):
            state["agent_online"] = True
            if display:
                display.show_status("agent online")
    msg_type = message.get("type")
    if msg_type == "heartbeat":
        payload = message.get("payload", {})
        host_ts_ms = payload.get("host_ts_ms") if isinstance(payload, dict) else None
        if (
            state is not None
            and payload.get("source") == "host"
            and isinstance(host_ts_ms, int)
        ):
            state["last_host_ts_ms"] = host_ts_ms
            state["last_host_clock_seen_ms"] = state["last_brain_seen_ms"]
        return
    if msg_type == "intent":
        execute_intent(message, actions, safety, protocol, ws, motion, reflex, state)


def check_brain_timeout(state, display, motion):
    last_seen = state.get("last_brain_seen_ms")
    if last_seen is None:
        return
    if ticks_diff(now_ms(), last_seen) <= BRAIN_HEARTBEAT_TIMEOUT_MS:
        return
    mark_agent_offline(state, display, motion, "brain_timeout")


def mark_agent_offline(state, display, motion, reason):
    was_online = state.get("agent_online", False)
    state["agent_online"] = False
    if was_online:
        display.show_status("agent offline")
    if motion:
        motion.cancel(reason)


async def network_loop(display, power, imu, actions, safety, protocol, state, motion, reflex):
    connection_worker = ConnectionWorker(lambda: WebSocketClient(AGENT_WS_URL, token=DEVICE_TOKEN))
    while True:
        if not await connect_wifi_async(WIFI_SSID, WIFI_PASSWORD, WIFI_CONNECT_TIMEOUT_MS):
            display.show_status("wifi offline")
            await sleep_ms(WS_RECONNECT_DELAY_MS)
            continue

        ws = await wait_for_connection(connection_worker)
        if ws is None:
            display.show_status("agent offline")
            await sleep_ms(WS_RECONNECT_DELAY_MS)
            continue

        state["ws"] = ws
        state["agent_online"] = True
        state["last_brain_seen_ms"] = now_ms()
        display.show_status("agent online")
        ws.send_json(protocol.status(power, imu, reflex, motion, state))
        last_heartbeat = now_ms()

        while ws.connected:
            try:
                if ws.tx_pending and not ws.flush_tx_chunk():
                    ws.close()
                    break
                message = ws.recv_json()
                if message:
                    handle_agent_message(message, actions, safety, protocol, ws, motion, reflex, state, display)
                check_brain_timeout(state, display, motion)
                if ticks_diff(now_ms(), last_heartbeat) >= FIRMWARE_HEARTBEAT_INTERVAL_MS:
                    if not ws.send_json(protocol.heartbeat(power, imu, reflex, motion, state)):
                        ws.close()
                        break
                    last_heartbeat = now_ms()
            except Exception as exc:
                print("network loop error:", exc)
                ws.close()
            await sleep_ms(100)

        state["ws"] = None
        mark_agent_offline(state, display, motion, "control_disconnected")
        await sleep_ms(WS_RECONNECT_DELAY_MS)


async def main():
    shared_i2c = SharedI2C.get()
    display = Display(i2c=shared_i2c)
    imu = IMU(i2c=shared_i2c)
    power = PowerMonitor(i2c=shared_i2c)
    touch = TouchInputs(i2c=shared_i2c)
    servos = ServoBank(i2c=shared_i2c)
    buzzer = Buzzer()
    actions = ActionPlayer(servos, display, buzzer)
    safety = SafetyGuard(power, imu)
    reflex = ReflexController(power, imu, actions, display, buzzer)
    protocol = FirmwareProtocol(SESSION_ID)
    media_worker = create_default_worker()
    media_state = media_worker.snapshot()
    network_state = {
        "ws": None,
        "agent_online": False,
        "last_brain_seen_ms": None,
        "last_host_ts_ms": None,
        "last_host_clock_seen_ms": None,
        "local_loop_ms": 0,
        "local_loop_interval_ms": 0,
        "local_loop_overrun_ms": 0,
        "servo_output_enabled": servos.enabled,
        "media": media_state,
    }

    def _on_intent_done(intent_id, status, reason=""):
        ws = network_state.get("ws")
        if ws and ws.connected:
            ws.send_json(protocol.ack(intent_id, status, reason if reason else None))

    motion = MotionController(actions, safety, reflex, now_ms, on_intent_done=_on_intent_done)
    fallback = LocalFallback(display, actions)
    adapter = EventAdapter(touch, imu, power)

    display.set_face("idle")
    print("naobot firmware booted; agent:", AGENT_WS_URL)
    asyncio.create_task(network_loop(display, power, imu, actions, safety, protocol, network_state, motion, reflex))
    media_worker.start()

    previous_loop_start = None
    try:
        while True:
            loop_start = now_ms()
            power.sample()
            imu.read_posture()
            network_state["servo_output_enabled"] = servos.enabled
            network_state["media"] = media_worker.snapshot()
            if previous_loop_start is not None:
                network_state["local_loop_interval_ms"] = max(
                    0, ticks_diff(loop_start, previous_loop_start)
                )
            previous_loop_start = loop_start
            if reflex.check():
                motion.cancel("reflex")
                reflex.run()
            motion.tick()
            event = adapter.poll()
            if event:
                media_worker.request_event_boost(ticks_add(now_ms(), MEDIA_EVENT_BOOST_MS))
                if not safety.can_emit_event(event):
                    actions.stop()
                    display.set_face("alert")
                else:
                    ws = network_state.get("ws")
                    envelope = protocol.event(event, power, imu, reflex, motion, network_state)
                    if not ws or not ws.connected or not ws.send_json(envelope):
                        fallback.handle(event)
            await sleep_to_safety_deadline(loop_start, network_state)
    finally:
        media_worker.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print("fatal:", exc)
