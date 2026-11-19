import tarfile
import os
import importlib
import importlib.util
import logging
import re
import subprocess
import sys

from PyQt5.QtCore import QThread, pyqtSignal  # 按你实际用的 Qt 绑定调整

_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(==[A-Za-z0-9._]+)?$")
_MOD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

from ota.config import (
    RELEASE_DIR,
    CURRENT_LINK,
    RESOURCE_DIR,
    RESOURCE_GUI_DIR,
    CONFIG_DIR,
    USER_DATA_DIR,
)

def install_package(package_path, version):
    target = os.path.join(RELEASE_DIR, version)
    os.makedirs(target, exist_ok=True)

    package_path_code = os.path.join(package_path)
    with tarfile.open(package_path_code, "r:gz") as tar:
        tar.extractall(target)

    resource_link = os.path.join(target, "resources")
    resource_gui_link = os.path.join(target, "GUI/resources")
    config_link = os.path.join(target, "GUI/data/config")
    user_data_link = os.path.join(target, "user_data")
    # user_data 做目录软链（授权态/画像缓存跨版本保留）：
    # 若安装包里自带同名真实目录，先备份再建链
    if os.path.isdir(user_data_link) and not os.path.islink(user_data_link):
        os.rename(user_data_link, user_data_link + ".pkg.bak")
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    subprocess.run(["ln", "-sfn", RESOURCE_DIR, resource_link])
    subprocess.run(["ln", "-sfn", RESOURCE_GUI_DIR, resource_gui_link])
    subprocess.run(["ln", "-sfn", CONFIG_DIR, config_link])
    subprocess.run(["ln", "-sfnT", USER_DATA_DIR, user_data_link], check=True)
    if os.path.realpath(user_data_link) != os.path.realpath(USER_DATA_DIR):
        raise RuntimeError(f"user_data link does not point to {USER_DATA_DIR}: {user_data_link}")

    return target

# (Python 导入名, pip 包名)
DEFAULT_PACKAGES = []

def check_and_install_packages(packages=DEFAULT_PACKAGES, timeout=120, retries=2):
    """检查并安装缺失的包。

    packages: 可迭代对象,元素为 (导入名, pip 包名) 元组。
              也可以直接传字符串,表示导入名和包名相同(如 "requests")。
    timeout:  单个包安装的最长等待秒数。
    retries:  pip 自带的网络重试次数。
    返回: 全部可用返回 True,否则 False。
    """
    ok = True
    for item in packages:
        # 兼容 "requests" 这种导入名 == 包名的简写
        module_name, package_name = (item, item) if isinstance(item, str) else item

        if not (_MOD_RE.match(module_name) and _PKG_RE.match(package_name)):
            logging.error("非法依赖项,已跳过: %r", item)
            ok = False
            continue

        if importlib.util.find_spec(module_name) is not None:
            logging.info("[OK] %s 已安装", package_name)
            continue

        logging.info("[INSTALL] 未检测到 %s,正在安装...", package_name)
        try:
            subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    package_name,
                    "--break-system-packages",
                    "--disable-pip-version-check",
                    "--retries", str(retries),
                    "--timeout", "15",
                ],
                check=True,
                timeout=timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            logging.error("[ERROR] %s 安装超时(%ss)", package_name, timeout)
            ok = False
            continue
        except subprocess.CalledProcessError as e:
            logging.error("[ERROR] %s 安装失败: %s\n%s",
                          package_name, e, (e.stderr or "").strip())
            ok = False
            continue

        importlib.invalidate_caches()
        if importlib.util.find_spec(module_name) is None:
            logging.error("[ERROR] %s 安装后仍无法导入", package_name)
            ok = False
            continue

        logging.info("[OK] %s 安装成功", package_name)

    return ok

def switch_version(version):
    target = os.path.join(RELEASE_DIR, version)
    subprocess.run(["ln", "-sfn", target, CURRENT_LINK])
    subprocess.run(["sudo", "reboot"])

class DependencyThread(QThread):
    result_ready = pyqtSignal(bool)

    def __init__(self, packages, parent=None):
        super().__init__(parent)
        self.packages = packages

    def run(self):
        self.result_ready.emit(check_and_install_packages(self.packages))