def run_fall_reflex(actions, display, buzzer=None):
    actions.emergency_stop()
    display.set_face("alert")
    if buzzer:
        buzzer.chirp("alert")
    return "fall_emergency_off"
