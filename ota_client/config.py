OTA_SERVER = "https://api.mindora316.com/ota/version.json"
# OTA_SERVER = "http://192.168.0.18:8000/ota/version.json"

DOWNLOAD_DIR = "/home/rpdzkj/update/download"

RELEASE_DIR = "/home/rpdzkj/releases"

RESOURCE_DIR = "/home/rpdzkj/resources/resources"

RESOURCE_GUI_DIR = "/home/rpdzkj/resources/resources_gui"

CONFIG_DIR = "/home/rpdzkj/config.json"

CURRENT_LINK = "/home/rpdzkj/mindora"

# 更新状态文件：记录 pending 更新与上一个 release，
# 供 boot 健康检查脚本（mindora 仓库，超出本目录）判断是否回滚
UPDATE_STATE_FILE = "/home/rpdzkj/update/state.json"

# 出厂预置的 OTA 签名密钥（HMAC-SHA256）。
# 文件存在则 version.json / manifest.json 强制验签；不存在则跳过（兼容旧设备）
SIGN_KEY_FILE = "/home/rpdzkj/ota_sign.key"
