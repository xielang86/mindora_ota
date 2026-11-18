import os

import requests
from PyQt5.QtCore import QThread
from PyQt5.QtCore import pyqtSignal

from ota.utils import verify_sha256


class DownloadThread(QThread):

    progress = pyqtSignal(int)

    finished = pyqtSignal(str)

    error = pyqtSignal(str)

    def __init__(self, url, save_path, sha256=None, retry=3):
        """下载单个文件（如 mindora.tar.gz）。

        sha256: 可选，下载完成后校验，不匹配走 error 信号。
        断点续传：目标文件已存在时从已下载字节处用 Range 继续，
        服务器不支持 Range（回 200）则自动从头重下。
        """
        super().__init__()

        self.url = url
        self.save_path = save_path
        self.sha256 = sha256
        self.retry = retry

        os.makedirs(os.path.dirname(self.save_path), exist_ok=True)

    def run(self):
        try:
            self._download_with_retry()

            if self.sha256 and not verify_sha256(self.save_path, self.sha256):
                raise RuntimeError(f"sha256 mismatch: {self.save_path}")

            self.finished.emit(self.save_path)
        except Exception as e:
            self.error.emit(str(e))

    def _download_with_retry(self):
        last = None

        for _ in range(self.retry):
            try:
                self._download_once()
                return
            except Exception as e:
                last = e

        raise last

    def _download_once(self):
        headers = {"Accept-Encoding": "identity"}

        existing = 0
        if os.path.exists(self.save_path):
            existing = os.path.getsize(self.save_path)
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"

        # connect timeout 10s，read timeout 300s，避免网络挂死线程永久卡住
        with requests.get(self.url, stream=True, headers=headers,
                          timeout=(10, 300)) as r:

            if existing and r.status_code == 200:
                # 服务器不支持断点续传，从头下载
                existing = 0

            r.raise_for_status()

            total = int(r.headers.get("content-length", 0)) + existing
            downloaded = existing

            mode = "ab" if existing else "wb"

            with open(self.save_path, mode) as f:
                for chunk in r.iter_content(1024 * 64):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        self.progress.emit(int(downloaded * 100 / total))
