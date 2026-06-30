from time import sleep_ms

from machine import PWM, Pin

# =========================
# 基本配置
# =========================

SERVO_PIN = 13      # 舵机黄色/橙色信号线接 GPIO13
SERVO_FREQ = 50     # 舵机常用 50Hz，也就是 20ms 一个周期

# 先用保守范围，避免小舵机顶死
# 1000us 大约一边，1500us 中位，2000us 另一边
MIN_US = 1000
MAX_US = 2000


# =========================
# 初始化舵机 PWM
# =========================

servo = PWM(Pin(SERVO_PIN), freq=SERVO_FREQ)


def angle_to_us(angle):
    """
    把角度 0~180 转换成脉冲宽度 1000~2000us
    """
    if angle < 0:
        angle = 0
    if angle > 180:
        angle = 180

    pulse_us = MIN_US + (MAX_US - MIN_US) * angle // 180
    return pulse_us


def write_servo(angle):
    """
    控制舵机转到指定角度
    """
    pulse_us = angle_to_us(angle)

    try:
        # 新版 MicroPython 推荐用 duty_ns，单位是纳秒
        servo.duty_ns(pulse_us * 1000)
    except AttributeError:
        # 如果你的 MicroPython 固件较旧，不支持 duty_ns，就用 duty_u16
        # 50Hz 周期 = 20ms = 20000us
        duty = int(pulse_us * 65535 // 20000)
        servo.duty_u16(duty)

    print("angle =", angle, "pulse_us =", pulse_us)


# =========================
# 主程序
# =========================

print("ESP32-S3 MicroPython Servo Test")
print("Signal pin: GPIO13")

# 上电先回中位
write_servo(90)
sleep_ms(1000)

while True:
    print("running...")
    write_servo(30)
    sleep_ms(1000)

    write_servo(90)
    sleep_ms(1000)

    write_servo(150)
    sleep_ms(1000)

    write_servo(90)
    sleep_ms(1000)