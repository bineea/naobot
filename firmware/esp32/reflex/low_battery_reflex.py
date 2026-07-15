def run_low_battery_reflex(power, imu, actions, display, buzzer=None):
    actions.emergency_stop()
    display.set_face("sleepy")
    if buzzer:
        buzzer.chirp("low_battery")
    return "low_battery_stop"
