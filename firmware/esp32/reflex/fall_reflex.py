def run_fall_reflex(actions, display, buzzer=None):
    shutdown_succeeded = actions.emergency_stop()
    display.set_face("alert")
    if buzzer:
        buzzer.chirp("alert")
    return "fall_emergency_off" if shutdown_succeeded else "fall_shutdown_failed"
