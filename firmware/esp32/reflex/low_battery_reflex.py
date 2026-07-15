def run_low_battery_reflex(power, imu, actions, display, buzzer=None):
    shutdown_succeeded = actions.emergency_stop()
    display.set_face("sleepy")
    if buzzer:
        buzzer.chirp("low_battery")
    return "low_battery_stop" if shutdown_succeeded else "low_battery_shutdown_failed"
