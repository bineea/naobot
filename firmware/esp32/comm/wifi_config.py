try:
    import network
except ImportError:
    network = None

try:
    import utime as time
except ImportError:
    import time

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio


_wlan = None


def _ticks_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def _ticks_diff(end, start):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(end, start)
    return end - start


def _sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000)


def get_wlan():
    global _wlan
    if not network:
        return None
    if _wlan is None:
        _wlan = network.WLAN(network.STA_IF)
    return _wlan


def connect_wifi(ssid, password, timeout_ms=12000):
    if not ssid or ssid == "YOUR_WIFI":
        print("wifi not configured")
        return False
    wlan = get_wlan()
    if not wlan:
        print("network module unavailable")
        return False
    wlan.active(True)
    if wlan.isconnected():
        print("wifi already connected:", wlan.ifconfig())
        return True

    print("wifi connecting:", ssid)
    wlan.connect(ssid, password)
    start = _ticks_ms()
    while _ticks_diff(_ticks_ms(), start) < timeout_ms:
        if wlan.isconnected():
            print("wifi connected:", wlan.ifconfig())
            return True
        _sleep_ms(250)

    print("wifi connect timeout")
    return False


async def connect_wifi_async(ssid, password, timeout_ms=12000, sleeper=None):
    if not ssid or ssid == "YOUR_WIFI":
        print("wifi not configured")
        return False
    wlan = get_wlan()
    if not wlan:
        print("network module unavailable")
        return False
    wlan.active(True)
    if wlan.isconnected():
        print("wifi already connected:", wlan.ifconfig())
        return True

    print("wifi connecting:", ssid)
    wlan.connect(ssid, password)
    start = _ticks_ms()
    while _ticks_diff(_ticks_ms(), start) < timeout_ms:
        if wlan.isconnected():
            print("wifi connected:", wlan.ifconfig())
            return True
        if sleeper is not None:
            await sleeper(50)
        elif hasattr(asyncio, "sleep_ms"):
            await asyncio.sleep_ms(50)
        else:
            await asyncio.sleep(0.05)

    print("wifi connect timeout")
    return False


def is_connected():
    wlan = get_wlan()
    return bool(wlan and wlan.isconnected())
