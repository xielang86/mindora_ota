import hashlib
import logging
import os
import random
import re
import shutil
import time

import requests
from PyQt5.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

# 可短暂重试的状态码(其中网关类可能是服务下线)
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
# 视为"服务不可用"的状态码: 重试耗尽后发 serviceUnavailable
UNAVAILABLE_STATUS = {404, 410, 502, 503, 504}


class DownloadCancelled(Exception):
    pass


class ServiceUnavailable(Exception):
    """服务端不可用(OTA 已关闭、网关故障、文件已撤回、无法连接等)。"""
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status        # HTTP 状态码,网络层错误为 0


class DownloadError(Exception):
    """带错误码的下载异常,便于界面区分提示。"""
    NETWORK = 1       # 网络 / 超时
    HTTP = 2          # HTTP 状态错误
    INCOMPLETE = 3    # 长度不符
    CHECKSUM = 4      # sha256 不匹配
    DISK = 5          # 磁盘空间不足 / 写入失败

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class DownloadThread(QThread):
    sourceSelected = pyqtSignal(str)
    progress = pyqtSignal(int)
    downloadFinished = pyqtSignal(str)
    error = pyqtSignal(str)
    # (HTTP 状态码,网络层错误为 0; 说明文本)
    serviceUnavailable = pyqtSignal(int, str)

    CHUNK_SIZE = 256 * 1024
    TIMEOUT = (10, 30)
    BACKOFF_MAX = 30
    # 服务不可用类错误最多重试几次(含首次),之后直接判定服务关闭
    UNAVAILABLE_TRIES = 2
    DISK_RESERVE = 50 * 1024 * 1024

    def __init__(self, url, save_path, retry=5, sha256=None, parent=None, info=None, *, device_headers=None):
        super().__init__(parent)
        self.info = info
        self.device_headers = device_headers
        self.selected_source = url
        self.url = url
        self.save_path = os.path.abspath(save_path)
        self.part_path = self.save_path + '.part'
        self.retry = max(int(retry), 1)
        self.sha256 = sha256.lower() if sha256 else None
        self._stop_flag = False

    # ===================== 取消 =====================

    def stop(self):
        self._stop_flag = True

    def _check_stop(self):
        if self._stop_flag or self.isInterruptionRequested():
            raise DownloadCancelled('download cancelled')

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check_stop()
            time.sleep(0.1)

    # ===================== 主流程 =====================

    def run(self):
        try:
            os.makedirs(os.path.dirname(self.save_path), exist_ok=True)
            from ota.sources import candidates, probe, check_urls
            from ota.sources import settings
            multi_source = bool(self.info and (len(check_urls(self.info.get('_check_url', ''))) > 1
                                               or self.info.get('download_urls')))
            urls = candidates(self.info, self.device_headers) if multi_source else [self.url]
            usable, slow = [], []
            if multi_source:
                threshold = float(settings().get('min_bytes_per_second', 32768))
                for url in urls:
                    self._check_stop()
                    try:
                        speed = probe(url, int(self.info['size']), self._check_stop, self.device_headers)
                        (usable if speed >= threshold else slow).append(url)
                        logger.info('OTA probe %s: %.0f bytes/s', url, speed)
                    except DownloadCancelled:
                        raise
                    except Exception as e:
                        logger.warning('OTA probe failed %s: %s', url, e)
                urls = usable + slow  # Slow but reachable remains usable on weak networks.
            last = RuntimeError('no reachable download source')
            for url in urls:
                self._check_stop()
                self.url = self.selected_source = url
                self.sourceSelected.emit(url)
                # Never append bytes from a different source or target.
                identity_path = self.part_path + '.identity'
                identity = url + '|' + str(self.sha256)
                try:
                    with open(identity_path) as f:
                        previous = f.read()
                except FileNotFoundError:
                    previous = ''
                if previous != identity:
                    self._remove_part()
                with open(identity_path, 'w') as f:
                    f.write(identity)
                try:
                    self._download_with_retry()
                    if self.info and os.path.getsize(self.part_path) != int(self.info['size']):
                        raise DownloadError(DownloadError.INCOMPLETE, 'size mismatch')
                    if self.sha256 and self._sha256(self.part_path) != self.sha256:
                        self._remove_part()
                        raise DownloadError(DownloadError.CHECKSUM, 'sha256 mismatch')
                    break
                except DownloadCancelled:
                    raise
                except DownloadError as e:
                    if e.code == DownloadError.DISK:
                        raise
                    last = e
                    logger.warning('switch download source after %s: %s', url, e)
                except Exception as e:
                    last = e
                    logger.warning('switch download source after %s: %s', url, e)
            else:
                raise last

            os.replace(self.part_path, self.save_path)

        except DownloadCancelled:
            self._remove_part()
            self.error.emit('download cancelled')

        except ServiceUnavailable as e:
            # 保留 .part,服务恢复后可以续传
            logger.warning('OTA service unavailable [%s]: %s', e.status, e)
            self.serviceUnavailable.emit(e.status, str(e))

        except DownloadError as e:
            logger.error('download failed [%d]: %s', e.code, e)
            self.error.emit(f'error occurred: {e.code}')

        except Exception as e:
            logger.exception('download failed: %s', self.url)
            self.error.emit(str(e))

        else:
            self.downloadFinished.emit(self.save_path)

    # ===================== 下载 =====================

    def _download_with_retry(self):
        last = None
        unavailable_count = 0
        attempt = 0

        while attempt < self.retry:
            try:
                self._download_once()
                return

            except (DownloadCancelled, ServiceUnavailable):
                raise

            except DownloadError as e:
                if e.code == DownloadError.DISK:
                    raise
                last = e

            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code in UNAVAILABLE_STATUS:
                    unavailable_count += 1
                    last = e
                    # 404/410: 文件不存在,重试没有意义,直接通知
                    if code in (404, 410) or unavailable_count >= self.UNAVAILABLE_TRIES:
                        raise ServiceUnavailable(code, f'HTTP {code}: {self.url}')
                elif code in RETRY_STATUS:
                    last = e
                else:
                    raise DownloadError(DownloadError.HTTP, str(e))

            except requests.ConnectionError as e:
                # 连不上服务器: 同样按"服务不可用"快速判定
                unavailable_count += 1
                last = e
                if unavailable_count >= self.UNAVAILABLE_TRIES:
                    raise ServiceUnavailable(0, f'cannot connect: {e}')

            except (requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as e:
                last = e

            except OSError as e:
                raise DownloadError(DownloadError.DISK, str(e))

            attempt += 1
            logger.warning('download failed (%d/%d): %s',
                           attempt, self.retry, last)
            if attempt < self.retry:
                if unavailable_count:
                    self._sleep(1 + random.random() * 2)    # 快速探测,1~3 秒
                else:
                    self._sleep(min(self.BACKOFF_MAX, 2 ** attempt)
                                + random.random())

        if isinstance(last, DownloadError):
            raise last
        raise DownloadError(DownloadError.NETWORK, str(last))

    def _download_once(self):
        pos = os.path.getsize(self.part_path) if os.path.exists(self.part_path) else 0
        headers = dict(self.device_headers or {})
        headers['Accept-Encoding'] = 'identity'
        if pos:
            headers['Range'] = f'bytes={pos}-'

        with requests.get(self.url, stream=True, timeout=self.TIMEOUT,
                          headers=headers) as r:

            if r.status_code == 416:
                if self.info and pos == int(self.info['size']):
                    return  # Complete part still goes through size and SHA256 checks.
                self._remove_part()
                raise DownloadError(DownloadError.INCOMPLETE, 'invalid Range position')

            r.raise_for_status()

            if r.status_code == 206:
                m = re.search(r'/(\d+)$', r.headers.get('Content-Range', ''))
                start = re.match(r'bytes (\d+)-', r.headers.get('Content-Range', ''))
                if not start or int(start[1]) != pos:
                    self._remove_part()
                    raise DownloadError(DownloadError.INCOMPLETE, 'Range offset mismatch')
                total = int(m.group(1)) if m else 0
                mode = 'ab'
            else:
                total = int(r.headers.get('Content-Length', 0))
                pos = 0
                mode = 'wb'

            self._check_disk(total - pos if total else 0)

            downloaded = pos
            last_pct = -1

            with open(self.part_path, mode) as f:
                for chunk in r.iter_content(self.CHUNK_SIZE):
                    self._check_stop()
                    if not chunk:
                        continue

                    f.write(chunk)
                    downloaded += len(chunk)

                    if total:
                        pct = downloaded * 100 // total
                        if pct != last_pct:
                            last_pct = pct
                            self.progress.emit(pct)

                f.flush()
                os.fsync(f.fileno())

        if total and downloaded != total:
            raise DownloadError(DownloadError.INCOMPLETE,
                                f'incomplete download: {downloaded}/{total}')

    # ===================== 工具 =====================

    def _check_disk(self, need):
        if not need:
            return
        free = shutil.disk_usage(os.path.dirname(self.save_path)).free
        if free < need + self.DISK_RESERVE:
            raise DownloadError(DownloadError.DISK,
                                f'no space: need {need}, free {free}')

    @staticmethod
    def _sha256(filename):
        h = hashlib.sha256()
        with open(filename, 'rb') as f:
            for block in iter(lambda: f.read(1024 * 1024), b''):
                h.update(block)
        return h.hexdigest()

    def _remove_part(self):
        try:
            os.remove(self.part_path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning('remove %s failed: %s', self.part_path, e)
