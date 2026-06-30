import time

import ssd1306
from machine import I2C, Pin

# 按你的实际接线修改
SCL = 9
SDA = 8

# OLED 参数
WIDTH = 128
HEIGHT = 64

# 如果你的 I2C 扫描结果是 0x3d，就改成 0x3D
OLED_ADDR = 0x3C

i2c = I2C(0, scl=Pin(SCL), sda=Pin(SDA), freq=400000)

oled = ssd1306.SSD1306_I2C(WIDTH, HEIGHT, i2c, addr=OLED_ADDR)

oled.fill(0)
oled.text("ESP32-S3", 0, 0)
oled.text("MicroPython", 0, 16)
oled.text("OLED OK!", 0, 32)
oled.show()

time.sleep(2)

count = 0

while True:
    oled.fill(0)
    oled.text("ESP32-S3 OLED", 0, 0)
    oled.text("Count:", 0, 20)
    oled.text(str(count), 56, 20)
    oled.show()

    count += 1
    time.sleep(1)