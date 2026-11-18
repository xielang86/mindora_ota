import re

import requests

from ota.config import OTA_SERVER, SIGN_KEY_FILE
from ota.utils import verify_signed_json, load_sign_key


def compare_version(v1, v2):
    """比较版本号，v1 新返回 1，v2 新返回 -1，相同返回 0。

    按 . - + _ 分段，数字段按数值比较，非数字段按字符串比较，
    容忍 "1.2.0-rc1" 这类写法，不再对非数字段直接 crash。
    """
    def parse(v):
        parts = []
        for seg in re.split(r"[.\-+_]", str(v)):
            if not seg:
                continue
            if seg.isdigit():
                parts.append((1, int(seg), ""))
            else:
                parts.append((0, 0, seg))
        return parts

    a = parse(v1)
    b = parse(v2)

    # 补齐长度
    max_len = max(len(a), len(b))
    pad = (1, 0, "")
    a += [pad] * (max_len - len(a))
    b += [pad] * (max_len - len(b))

    for x, y in zip(a, b):
        if x > y:
            return 1   # v1更新
        elif x < y:
            return -1  # v2更新

    return 0           # 相同


def _verify_signature(data):
    """设备上存在签名密钥则强制验签，不存在则跳过（兼容未预置密钥的旧设备）"""
    key = load_sign_key(SIGN_KEY_FILE)
    if key is None:
        return True
    return verify_signed_json(data, key)


def check_version():
    try:
        r = requests.get(OTA_SERVER, timeout=10)
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.Timeout:
        return False, "error occurred: 1"              # 超时
    except requests.exceptions.ConnectionError:
        return False, "error occurred: 2"             # 无法连接服务器
    except requests.exceptions.HTTPError:
        return False, "error occurred: 3"             # HTTP 错误（404、500 等）
    except requests.exceptions.RequestException:
        return False, "error occurred: 4"              # 其他请求错误
    except ValueError:
        return False, "error occurred: 6"             # 响应不是合法 JSON

    if not _verify_signature(data):
        return False, "error occurred: 5"             # 签名校验失败

    return True, data


def check_manifest(url):
    """拉取 resources 的 manifest.json 并验签，返回 (ok, manifest_or_error)"""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        manifest = resp.json()
    except requests.exceptions.RequestException as e:
        return False, f"error occurred: {e}"
    except ValueError:
        return False, "error occurred: invalid manifest json"

    if not _verify_signature(manifest):
        return False, "error occurred: manifest signature mismatch"

    return True, manifest
