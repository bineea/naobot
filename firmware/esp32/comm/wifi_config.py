try:
    import network
except ImportError:
    network = None


def connect_wifi(ssid, password):
    if not network:
        print("network module unavailable")
        return False
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(ssid, password)
    return wlan.isconnected()
