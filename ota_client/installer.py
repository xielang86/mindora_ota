import json
import os
import shutil
import subprocess
import tarfile

from ota.config import (
    RELEASE_DIR,
    CURRENT_LINK,
    RESOURCE_DIR,
    RESOURCE_GUI_DIR,
    CONFIG_DIR,
    UPDATE_STATE_FILE
)
from ota.utils import verify_sha256


def _safe_extract(tar, target):
    """安全解压，防 tar 路径穿越（CVE-2007-4559）。

    Python 3.11.4+/3.12+ 用内置 data filter；
    低版本手工检查每个成员的路径和链接目标不能逃出 target。
    """
    if hasattr(tarfile, "data_filter"):
        tar.extractall(target, filter="data")
        return

    base = os.path.realpath(target)

    for m in tar.getmembers():
        dest = os.path.realpath(os.path.join(base, m.name))

        if dest != base and not dest.startswith(base + os.sep):
            raise RuntimeError(f"unsafe path in tar: {m.name}")

        if m.issym() or m.islnk():
            link_dest = os.path.realpath(
                os.path.join(os.path.dirname(dest), m.linkname)
            )
            if not link_dest.startswith(base + os.sep):
                raise RuntimeError(
                    f"unsafe link in tar: {m.name} -> {m.linkname}"
                )

    tar.extractall(target)


def _check_disk_space(tar, target_dir):
    """解压前预估所需空间（tar 内文件总大小），留 10% 余量"""
    needed = sum(m.size for m in tar.getmembers() if m.isfile())
    free = shutil.disk_usage(target_dir).free

    if needed > free * 0.9:
        raise RuntimeError(
            f"not enough disk space: need {needed}, free {free}"
        )


def install_package(package_path, version, sha256=None):
    """解压整包到 releases/<version> 并挂接共享资源目录。

    sha256: 可选，解压前再校验一次包完整性（纵深防御，
    防止下载与安装之间文件被改动/损坏）。
    """
    if sha256 and not verify_sha256(package_path, sha256):
        raise RuntimeError(f"sha256 mismatch: {package_path}")

    target = os.path.join(RELEASE_DIR, version)
    os.makedirs(target, exist_ok=True)

    with tarfile.open(package_path, "r:gz") as tar:
        _check_disk_space(tar, RELEASE_DIR)
        _safe_extract(tar, target)

    resource_link = os.path.join(target, "resources")
    resource_gui_link = os.path.join(target, "GUI/resources")
    config_link = os.path.join(target, "GUI/data/config")
    subprocess.run(["ln", "-sfn", RESOURCE_DIR, resource_link], check=True)
    subprocess.run(["ln", "-sfn", RESOURCE_GUI_DIR, resource_gui_link], check=True)
    subprocess.run(["ln", "-sfn", CONFIG_DIR, config_link], check=True)

    return target


def switch_version(version, reboot=True):
    """切换 CURRENT_LINK 到新版本。

    切换前把当前/上一个 release 写入 UPDATE_STATE_FILE（pending=true），
    供 boot 健康检查使用：新版本启动联网上报成功后调用
    mark_update_success()；若连续 N 次启动未标记，
    启动脚本调用 rollback() 指回上一个 release。
    """
    target = os.path.join(RELEASE_DIR, version)

    if not os.path.isdir(target):
        raise RuntimeError(f"release not found: {target}")

    previous = None
    if os.path.islink(CURRENT_LINK):
        previous = os.path.realpath(CURRENT_LINK)

    _write_state({
        "pending": True,
        "current": target,
        "previous": previous,
        "boot_count": 0
    })

    subprocess.run(["ln", "-sfn", target, CURRENT_LINK], check=True)

    if reboot:
        subprocess.run(["sudo", "reboot"])


def mark_update_success():
    """新版本运行正常后调用，清除 pending 状态"""
    state = _read_state()
    if not state:
        return

    state["pending"] = False
    state["boot_count"] = 0
    _write_state(state)


def rollback():
    """把 CURRENT_LINK 指回上一个 release 并清除 pending。

    返回是否执行了回滚（无 previous 时无能为力）。
    """
    state = _read_state()
    previous = state.get("previous") if state else None

    if not previous or not os.path.isdir(previous):
        return False

    subprocess.run(["ln", "-sfn", previous, CURRENT_LINK], check=True)

    state["pending"] = False
    state["rolled_back"] = True
    _write_state(state)

    return True


def incr_boot_count():
    """每次启动调用一次，返回当前 boot_count（供健康检查判断阈值）"""
    state = _read_state()
    if not state or not state.get("pending"):
        return 0

    state["boot_count"] = state.get("boot_count", 0) + 1
    _write_state(state)

    return state["boot_count"]


def _read_state():
    try:
        with open(UPDATE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_state(state):
    os.makedirs(os.path.dirname(UPDATE_STATE_FILE), exist_ok=True)

    # 先写临时文件再原子替换，防止写一半断电留下半个 JSON
    tmp = UPDATE_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    os.replace(tmp, UPDATE_STATE_FILE)
