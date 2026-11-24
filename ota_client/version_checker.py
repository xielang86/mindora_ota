# import requests
# from PyQt5.QtCore import QThread
# from PyQt5.QtCore import pyqtSignal
# from ota.config import OTA_INFO_URL

# def compare_version(v1, v2):
#     def parse(v):
#         return [int(x) for x in v.split(".")]

#     a = parse(v1)
#     b = parse(v2)

#     # 补齐长度
#     max_len = max(len(a), len(b))
#     a += [0] * (max_len - len(a))
#     b += [0] * (max_len - len(b))

#     for i in range(max_len):
#         if a[i] > b[i]:
#             return 1   # v1更新
#         elif a[i] < b[i]:
#             return -1  # v2更新

#     return 0           # 相同   

# def check_version(serial_number, current_version):
#     try:
#         r = requests.post(
#             OTA_INFO_URL,
#             json={
#                 "serial_number": serial_number,
#                 "current_version": current_version
#             },
#             timeout=10
#         )
#         r.raise_for_status()
#         return True, r.json()
#     except requests.exceptions.Timeout:
#         return False, "error occurred: 1"          # 超时
#     except requests.exceptions.ConnectionError:
#         return False, "error occurred: 2"          # 无法连接服务器
#     except requests.exceptions.HTTPError:
#         return False, "error occurred: 3"          # HTTP 错误（404、500 等）
#     except requests.exceptions.RequestException:
#         return False, "error occurred: 4"          # 其他请求错误

# def checkManifest(url):
#     try:
#         r = requests.get(url, timeout=10)
#         r.raise_for_status()
#         return True, r.json()
#     except requests.exceptions.Timeout:
#         return False, "error occurred: 1"              # 超时
#     except requests.exceptions.ConnectionError:
#         return False, "error occurred: 2"             # 无法连接服务器
#     except requests.exceptions.HTTPError:
#         return False, "error occurred: 3"             # HTTP 错误（404、500 等）
#     except requests.exceptions.RequestException:
#         return False, "error occurred: 4"              # 其他请求错误

# class VersionCheckThread(QThread):
#     finished = pyqtSignal(bool, object)

#     def __init__(self, serial_number, current_version):
#         super().__init__()
#         self.serial_number = serial_number
#         self.current_version = current_version

#     def run(self):
#         try:
#             success, result = check_version(
#                 self.serial_number,
#                 self.current_version
#             )
#             self.finished.emit(success, result)
#         except Exception as e:
#             self.finished.emit(False, str(e))


import logging
import re
from enum import IntEnum

import requests
from PyQt5.QtCore import QThread, pyqtSignal

from ota.config import OTA_INFO_URL

logger = logging.getLogger(__name__)

# (连接超时, 读取超时)
REQUEST_TIMEOUT = (5, 10)


# ---------------------------------------------------------------- 版本比较
# _VERSION_RE = re.compile(r"^\s*[vV]?(\d+(?:\.\d+)*)")


# def _parse_version(v):
#     """'v1.2.19-beta' -> (1, 2, 19);末尾的 0 会被忽略,使 '1.0' == '1'。"""
#     m = _VERSION_RE.match(v) if isinstance(v, str) else None
#     if not m:
#         raise ValueError(f"invalid version: {v!r}")
#     nums = [int(x) for x in m.group(1).split(".")]
#     while nums and nums[-1] == 0:
#         nums.pop()
#     return tuple(nums)


# def compare_version(v1, v2):
#     """返回 1: v1 更新;-1: v2 更新;0: 相同。版本号非法时抛 ValueError。"""
#     a, b = _parse_version(v1), _parse_version(v2)
#     return (a > b) - (a < b)


# ---------------------------------------------------------------- 网络请求
class OtaError(IntEnum):
    TIMEOUT = 1       # 超时
    CONNECTION = 2    # 无法连接服务器
    HTTP = 3          # HTTP 错误(404、500 等)
    REQUEST = 4       # 其他请求错误
    INVALID_JSON = 5  # 响应不是合法 JSON
    UNKNOWN = 6       # 未预期的异常


def _error_text(code):
    # 与原格式保持一致,调用方无需修改
    return f"error occurred: {int(code)}"


def _request_json(method, url, **kwargs):
    """统一的请求入口。成功返回 (True, dict/list),失败返回 (False, 错误文本)。"""
    try:
        r = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        r.raise_for_status()
        return True, r.json()
    except requests.exceptions.Timeout:
        code = OtaError.TIMEOUT
    except requests.exceptions.ConnectionError:
        code = OtaError.CONNECTION
    except requests.exceptions.HTTPError:
        code = OtaError.HTTP
    except ValueError:  # JSON 解析失败(含 requests.JSONDecodeError)
        code = OtaError.INVALID_JSON
    except requests.exceptions.RequestException:
        code = OtaError.REQUEST
    except Exception:
        code = OtaError.UNKNOWN

    logger.exception("%s %s failed (%s)", method, url, code.name)
    return False, _error_text(code)


def check_version(serial_number, current_version, device_headers=None):
    from ota.sources import check_urls, get_info
    urls = check_urls(OTA_INFO_URL)
    if urls == [OTA_INFO_URL]:
        # Explicit single-source deployments retain the old error-code contract.
        success, result = _request_json('POST', OTA_INFO_URL, headers=device_headers, json={
            'serial_number': serial_number, 'current_version': current_version})
        if success and isinstance(result, dict):
            result['_check_url'] = OTA_INFO_URL
        return success, result
    last = None
    from packaging.version import Version, InvalidVersion
    first_valid = None
    for url in urls:
        try:
            info = get_info(url, serial_number, current_version, device_headers)
            try:
                newer = Version(info['version']) > Version(current_version)
            except (InvalidVersion, TypeError) as e:
                raise ValueError('invalid OTA version') from e
            info['_check_url'] = url
            if newer:
                return True, info
            if first_valid is None:
                first_valid = info
            logger.info('OTA source has no newer version; trying next: %s', url)
        except Exception as e:
            last = e
            logger.warning('OTA check source unavailable %s: %s', url, e)
    return (True, first_valid) if first_valid is not None else (False, str(last or 'No OTA source configured'))


def checkManifest(url):
    return _request_json("GET", url)


# ---------------------------------------------------------------- 线程
class _RequestThread(QThread):
    """基类:子类只需实现 _do_request()。

    注意不要命名为 finished,避免覆盖 QThread 自带信号。
    """
    result_ready = pyqtSignal(bool, object)  # (是否成功, 数据或错误文本)

    def _do_request(self):
        raise NotImplementedError

    def run(self):
        try:
            success, result = self._do_request()
        except Exception as e:  # 兜底,保证一定有信号发出
            logger.exception("%s crashed", type(self).__name__)
            success, result = False, str(e)

        # 调用方已请求中断(例如窗口关闭)时,不再回调
        if not self.isInterruptionRequested():
            self.result_ready.emit(success, result)


class VersionCheckThread(_RequestThread):
    def __init__(self, serial_number, current_version, parent=None, *, device_headers=None):
        super().__init__(parent)
        self.serial_number = serial_number
        self.current_version = current_version
        self.device_headers = device_headers

    def _do_request(self):
        return check_version(self.serial_number, self.current_version, self.device_headers)


class ManifestCheckThread(_RequestThread):
    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url

    def _do_request(self):
        return checkManifest(self.url)
