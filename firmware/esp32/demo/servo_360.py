from time import sleep_ms

from machine import PWM, Pin

# =========================
# 引脚配置
# =========================

SERVO_PIN = 13       # 舵机黄色/橙色信号线接 GPIO13
PWM_FREQ = 50        # 舵机标准 PWM 频率 50Hz，周期 20ms


# =========================
# 舵机参数
# =========================

# 连续旋转舵机的停止点通常在 1500us 附近
# 如果舵机在 1500us 仍然缓慢转动，就需要微调这个值
STOP_US = 1500

# 常用控制范围
MIN_US = 1000        # 一个方向最快
MAX_US = 2000        # 另一个方向最快


# =========================
# 初始化 PWM
# =========================

servo = PWM(Pin(SERVO_PIN))
servo.freq(PWM_FREQ)


def set_pulse_us(pulse_us):
    """
    直接设置 PWM 高电平脉宽，单位是微秒 us。
    360° 舵机靠这个值控制方向和速度。
    """
    if pulse_us < MIN_US:
        pulse_us = MIN_US

    if pulse_us > MAX_US:
        pulse_us = MAX_US

    try:
        # 新版 MicroPython 支持 duty_ns，精度更直观
        servo.duty_ns(pulse_us * 1000)
    except AttributeError:
        # 兼容旧版 MicroPython
        # 50Hz 周期 = 20ms = 20000us
        duty = int(pulse_us * 65535 / 20000)
        servo.duty_u16(duty)

    print("pulse_us =", pulse_us)


def stop():
    """
    停止舵机。
    """
    set_pulse_us(STOP_US)


def forward(speed=50):
    """
    正转。
    speed 范围：0~100
    0 表示接近停止，100 表示最快。
    """
    if speed < 0:
        speed = 0

    if speed > 100:
        speed = 100

    pulse_us = STOP_US + int((MAX_US - STOP_US) * speed / 100)
    set_pulse_us(pulse_us)


def backward(speed=50):
    """
    反转。
    speed 范围：0~100
    0 表示接近停止，100 表示最快。
    """
    if speed < 0:
        speed = 0

    if speed > 100:
        speed = 100

    pulse_us = STOP_US - int((STOP_US - MIN_US) * speed / 100)
    set_pulse_us(pulse_us)


# =========================
# 主程序
# =========================

print("ESP32-S3 360 Degree Servo Test")
print("Signal pin: GPIO13")

# 上电先停止
stop()
sleep_ms(1500)

while True:
    print("stop")
    stop()
    sleep_ms(1500)

    print("forward slow")
    forward(30)
    sleep_ms(2000)

    print("stop")
    stop()
    sleep_ms(1000)

    print("forward fast")
    forward(80)
    sleep_ms(2000)

    print("stop")
    stop()
    sleep_ms(1000)

    print("backward slow")
    backward(30)
    sleep_ms(2000)

    print("stop")
    stop()
    sleep_ms(1000)

    print("backward fast")
    backward(80)
    sleep_ms(2000)

    print("stop")
    stop()
    sleep_ms(2000)