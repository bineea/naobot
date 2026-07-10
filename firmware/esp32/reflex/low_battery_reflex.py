def run_low_battery_reflex(actions, display, buzzer=None):
    actions.stop()
    display.set_face("sleepy")
    if buzzer:
        buzzer.chirp("low_battery")
    actions.execute({"name": "sit", "args": {}})
    return "low_battery_sit"
