from machine import Pin, I2C
import time

MPU_ADDR = 0x68

i2c = I2C(
    0,
    scl=Pin(9),
    sda=Pin(8),
    freq=400000
)

gyro_offset_x = 0
gyro_offset_y = 0
gyro_offset_z = 0

acc_offset_x = 0
acc_offset_y = 0
acc_offset_z = 0

def write_reg(reg, value):
    i2c.writeto_mem(MPU_ADDR, reg, bytes([value]))

def read_word(reg):
    data = i2c.readfrom_mem(MPU_ADDR, reg, 2)
    value = (data[0] << 8) | data[1]

    if value >= 0x8000:
        value -= 65536

    return value

def init_mpu6050():
    # 唤醒 MPU6050
    write_reg(0x6B, 0x00)

    # 设置数字低通滤波器，减少抖动
    write_reg(0x1A, 0x03)

    # 加速度计量程 ±2g
    write_reg(0x1C, 0x00)

    # 陀螺仪量程 ±250 deg/s
    write_reg(0x1B, 0x00)

    time.sleep(0.2)

def read_raw_data():
    acc_x = read_word(0x3B)
    acc_y = read_word(0x3D)
    acc_z = read_word(0x3F)

    temp_raw = read_word(0x41)

    gyro_x = read_word(0x43)
    gyro_y = read_word(0x45)
    gyro_z = read_word(0x47)

    return acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z, temp_raw

def calibrate_mpu6050(samples=300):
    global gyro_offset_x, gyro_offset_y, gyro_offset_z
    global acc_offset_x, acc_offset_y, acc_offset_z

    print("开始校准，请保持 MPU6050 静止并平放...")

    gx_sum = 0
    gy_sum = 0
    gz_sum = 0

    ax_sum = 0
    ay_sum = 0
    az_sum = 0

    for i in range(samples):
        ax, ay, az, gx, gy, gz, temp_raw = read_raw_data()

        ax_sum += ax
        ay_sum += ay
        az_sum += az

        gx_sum += gx
        gy_sum += gy
        gz_sum += gz

        time.sleep(0.005)

    acc_offset_x = ax_sum / samples
    acc_offset_y = ay_sum / samples

    # 平放时 Z 轴应该约等于 1g，也就是 16384
    acc_offset_z = (az_sum / samples) - 16384

    gyro_offset_x = gx_sum / samples
    gyro_offset_y = gy_sum / samples
    gyro_offset_z = gz_sum / samples

    print("校准完成")
    print("Gyro offset:", gyro_offset_x, gyro_offset_y, gyro_offset_z)
    print("Acc offset:", acc_offset_x, acc_offset_y, acc_offset_z)

def read_mpu6050():
    ax_raw, ay_raw, az_raw, gx_raw, gy_raw, gz_raw, temp_raw = read_raw_data()

    # 减去校准偏移
    ax_raw -= acc_offset_x
    ay_raw -= acc_offset_y
    az_raw -= acc_offset_z

    gx_raw -= gyro_offset_x
    gy_raw -= gyro_offset_y
    gz_raw -= gyro_offset_z

    ax = ax_raw / 16384
    ay = ay_raw / 16384
    az = az_raw / 16384

    gx = gx_raw / 131
    gy = gy_raw / 131
    gz = gz_raw / 131

    temp = temp_raw / 340 + 36.53

    return ax, ay, az, gx, gy, gz, temp

devices = i2c.scan()
print("I2C devices:", devices)

if MPU_ADDR not in devices:
    print("未找到 MPU6050，请检查接线")
else:
    init_mpu6050()
    calibrate_mpu6050()

    while True:
        ax, ay, az, gx, gy, gz, temp = read_mpu6050()

        print("ACC: X={:.3f}g Y={:.3f}g Z={:.3f}g".format(ax, ay, az))
        print("GYRO: X={:.3f} Y={:.3f} Z={:.3f} deg/s".format(gx, gy, gz))
        print("TEMP: {:.2f} C".format(temp))
        print("-----------------------")

        time.sleep(0.5)