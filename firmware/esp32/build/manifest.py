include("$(PORT_DIR)/boards/manifest.py")

# 主程序及其余驱动仍可用 mpremote 快速迭代；媒体基础模块冻结进定制镜像。
module("../config.py")
package("boards", base_path="..")
package("media", base_path="..")
