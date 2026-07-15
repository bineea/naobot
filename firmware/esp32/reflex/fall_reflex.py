def run_fall_reflex(actions, display, buzzer=None):
    actions.stop()
    display.set_face("alert")
    if buzzer:
        buzzer.chirp("alert")
    return "brace_and_sit"
