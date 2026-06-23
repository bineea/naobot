try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

from config import AGENT_WS_URL
from hardware.display import Display
from hardware.imu import IMU
from hardware.power import PowerMonitor
from hardware.servo import ServoBank
from hardware.touch import TouchInputs
from interaction.event_adapter import EventAdapter
from interaction.local_fallback import LocalFallback
from motion.action_player import ActionPlayer
from safety.guard import SafetyGuard


async def main():
    display = Display()
    imu = IMU()
    power = PowerMonitor()
    touch = TouchInputs()
    servos = ServoBank()
    actions = ActionPlayer(servos, display)
    safety = SafetyGuard(power, imu)
    fallback = LocalFallback(display, actions)
    adapter = EventAdapter(touch, imu, power)

    display.set_face("idle")
    print("naobot firmware booted; agent:", AGENT_WS_URL)

    while True:
        event = adapter.poll()
        if event:
            if not safety.can_emit_event(event):
                actions.stop()
                display.set_face("alert")
            else:
                fallback.handle(event)
        await asyncio.sleep_ms(50)


try:
    asyncio.run(main())
except Exception as exc:
    print("fatal:", exc)
