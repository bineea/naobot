from config import BATTERY_CRITICAL_PCT


def _is_critical(power):
    if hasattr(power, "is_critical"):
        return power.is_critical()
    battery_pct = getattr(power, "battery_pct", None)
    return battery_pct is None or battery_pct <= BATTERY_CRITICAL_PCT


def run_low_battery_reflex(power, imu, actions, display, buzzer=None):
    actions.stop()
    safe_to_sit = (
        not _is_critical(power)
        and not imu.is_fault()
        and not actions.emergency_latched
    )
    sat_safely = False
    if safe_to_sit:
        result = actions.execute({"name": "sit", "args": {}})
        sat_safely = bool(result.accepted)
        actions.stop()
    display.set_face("sleepy")
    if buzzer:
        buzzer.chirp("low_battery")
    return "low_battery_sit" if sat_safely else "low_battery_off"
