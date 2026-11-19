from config.config import Config

OTA_INFO_URL = f"{Config.API_BASE_URL}/ota/check"
# Production defaults shared by every release. Test devices prepend LAN URLs
# through MINDORA_OTA_CHECK_URLS (a JSON array in the service environment).
OTA_CHECK_URLS = [
    "https://ota.mindora316.com/ota/check",
    OTA_INFO_URL,
]

DOWNLOAD_DIR = "/home/rpdzkj/update/download"

RELEASE_DIR = "/home/rpdzkj/releases"

RESOURCE_DIR = "/home/rpdzkj/resources/resources"

RESOURCE_GUI_DIR = "/home/rpdzkj/resources/resources_gui"

CONFIG_DIR = "/home/rpdzkj/config.json"

# 用户数据目录（auth.json / user.json）：版本外存储，OTA 切版本后授权态与画像缓存不丢
USER_DATA_DIR = "/home/rpdzkj/user_data"

CURRENT_LINK = "/home/rpdzkj/mindora"
