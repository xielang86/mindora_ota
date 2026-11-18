import os
import json
import shutil
import hashlib
import tempfile
import requests

from PyQt5.QtCore import QThread, pyqtSignal

from ota.utils import verify_signed_json, load_sign_key


class ResourceUpdater(QThread):
    progressChanged = pyqtSignal(int)
    statusChanged = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, resources_url, download_list, delete_list,
                 resource_root='/home/rpdzkj/resources', retry=3,
                 parent=None, sign_key_file=None):
        """资源增量更新（下载 -> 校验 -> 安装 -> 清理 -> 失败回滚）。

        sign_key_file: 可选，传入后下载的 manifest.json 强制验签。
        """
        super().__init__(parent)

        self.resources_url = resources_url.rstrip('/')
        self.download_list = download_list
        self.delete_list = delete_list
        self.resource_root = resource_root
        self.retry = retry
        self.sign_key_file = sign_key_file

        self.temp_dir = tempfile.mkdtemp(prefix='resource_ota_')
        self.download_dir = os.path.join(self.temp_dir, 'download')
        self.backup_dir = os.path.join(self.temp_dir, 'backup')

        self._stop = False

    # ===================== QThread 入口 =====================

    def run(self):
        try:
            self.statusChanged.emit('checking')
            self.check_disk_space()

            self.statusChanged.emit('downloading')
            self.download_all()

            self._check_stop()

            self.statusChanged.emit('verifying')
            self.verify_all()

            self._check_stop()

            self.statusChanged.emit('installing')
            self.install_all()

            self._check_stop()

            self.statusChanged.emit('cleanup')
            self.cleanup()

            self.finished.emit(True, '')

        except Exception as e:
            try:
                print("resource update failed, rollback:", e)
                self.rollback()
            finally:
                self.finished.emit(False, str(e))

        finally:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ===================== 取消 =====================

    def stop(self):
        self._stop = True

    def _check_stop(self):
        if self._stop or self.isInterruptionRequested():
            raise RuntimeError('update cancelled')

    # ===================== 工具 =====================

    @staticmethod
    def md5(filename):
        h = hashlib.md5()
        with open(filename, 'rb') as f:
            for block in iter(lambda: f.read(1024 * 1024), b''):
                h.update(block)
        return h.hexdigest()

    # ===================== 磁盘预检 =====================

    def check_disk_space(self):
        needed = sum(item.get('size', 0) for item in self.download_list)
        # 下载区 + 备份区最坏各一份，留 10% 余量
        required = int(needed * 2 * 1.1) + 10 * 1024 * 1024

        free = shutil.disk_usage(self.temp_dir).free

        if required > free:
            raise RuntimeError(
                f'not enough disk space: need {required}, free {free}'
            )

    # ===================== 下载 =====================

    def download_all(self):
        total = max(len(self.download_list), 1)

        session = requests.Session()

        for idx, item in enumerate(self.download_list):

            self._check_stop()

            url = f'{self.resources_url}/{item["path"]}'
            dst = os.path.join(self.download_dir, item['path'])

            os.makedirs(os.path.dirname(dst), exist_ok=True)

            self._download_file(session, url, dst, item.get('size'))

            self.progressChanged.emit(int((idx + 1) * 100 / total))

        # 下载 manifest
        manifest_url = f'{self.resources_url}/manifest.json'
        manifest_dst = os.path.join(self.temp_dir, 'manifest.json')

        with session.get(manifest_url, timeout=30) as r:
            r.raise_for_status()

            with open(manifest_dst, 'wb') as f:
                f.write(r.content)

        self._verify_manifest(manifest_dst)

    def _download_file(self, session, url, dst, expected_size):
        """单文件下载，支持断点续传（Range）+ 整文件重试"""
        last = None

        for _ in range(self.retry):
            self._check_stop()

            existing = os.path.getsize(dst) if os.path.exists(dst) else 0

            if expected_size is not None and existing > expected_size:
                # 比预期还大说明文件脏了，重来
                os.remove(dst)
                existing = 0

            headers = {'Accept-Encoding': 'identity'}
            if existing:
                headers['Range'] = f'bytes={existing}-'

            try:
                with session.get(url, stream=True, timeout=(10, 120),
                                 headers=headers) as r:
                    if existing and r.status_code == 200:
                        # 服务器不支持 Range，从头下载
                        existing = 0

                    r.raise_for_status()

                    mode = 'ab' if existing else 'wb'

                    with open(dst, mode) as f:
                        for chunk in r.iter_content(1024 * 256):
                            self._check_stop()

                            if chunk:
                                f.write(chunk)

                if (expected_size is not None
                        and os.path.getsize(dst) != expected_size):
                    raise RuntimeError(
                        f'{url} size mismatch, expect {expected_size}'
                    )

                return

            except Exception as e:
                last = e

        raise last

    # ===================== 校验 =====================

    def verify_all(self):
        for item in self.download_list:
            self._check_stop()

            f = os.path.join(self.download_dir, item['path'])

            if self.md5(f) != item['md5']:
                # 删掉坏文件，下次更新会重新完整下载
                os.remove(f)
                raise RuntimeError(f'{item["path"]} md5 mismatch')

    def _verify_manifest(self, manifest_path):
        if not self.sign_key_file:
            return

        key = load_sign_key(self.sign_key_file)
        if key is None:
            return

        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)

        if not verify_signed_json(manifest, key):
            raise RuntimeError('manifest.json signature mismatch')

    # ===================== 安装 =====================

    def install_all(self):
        os.makedirs(self.backup_dir, exist_ok=True)

        for item in self.download_list:

            self._check_stop()

            src = os.path.join(self.download_dir, item['path'])
            dst = os.path.join(self.resource_root, item['path'])

            os.makedirs(os.path.dirname(dst), exist_ok=True)

            if os.path.exists(dst):
                bk = os.path.join(self.backup_dir, item['path'])
                os.makedirs(os.path.dirname(bk), exist_ok=True)
                shutil.copy2(dst, bk)

            os.replace(src, dst)

        # 替换 manifest（最后一步，旧 manifest 一并备份以便回滚）
        src_manifest = os.path.join(self.temp_dir, 'manifest.json')
        dst_manifest = os.path.join(self.resource_root, 'manifest.json')

        if os.path.exists(dst_manifest):
            shutil.copy2(dst_manifest,
                         os.path.join(self.backup_dir, 'manifest.json'))

        os.replace(src_manifest, dst_manifest)

    # ===================== 清理 =====================

    def cleanup(self):
        for rel in self.delete_list:

            self._check_stop()

            p = os.path.join(self.resource_root, rel)

            if os.path.isfile(p):
                os.remove(p)

            d = os.path.dirname(p)

            while (
                d.startswith(self.resource_root)
                and os.path.isdir(d)
                and not os.listdir(d)
            ):
                os.rmdir(d)
                d = os.path.dirname(d)

    # ===================== 回滚 =====================

    def rollback(self):
        if not os.path.isdir(self.backup_dir):
            return

        for root, _, files in os.walk(self.backup_dir):
            for name in files:
                src = os.path.join(root, name)

                rel = os.path.relpath(src, self.backup_dir)
                dst = os.path.join(self.resource_root, rel)

                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
