
from machine import I2C, Pin

print("Program started")

SCL = 9
SDA = 8

print("Init I2C...")
i2c = I2C(0, scl=Pin(SCL), sda=Pin(SDA), freq=100000)

print("Scanning...")
devices = i2c.scan()

print("Scan done")

if devices:
    print("I2C devices:", [hex(d) for d in devices])
else:
    print("No I2C devices found")