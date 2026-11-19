import errno
import hashlib
import logging
import os
import shutil
import tempfile
import time

import requests
from PyQt5.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)


class UpdateCancelled(Exception):
    """用户取消更新。单独定义，避免被重试逻辑当成普通错误吞掉。"""


class ResourceUpdater(QThread):
    result_ready = pyqtSignal(bool, str)
    progressChanged = pyqtSignal(int)
    statusChanged = pyqtSignal(str)

    # 总进度分配 (起点, 跨度)
    _P_DOWNLOAD = (0, 70)
    _P_VERIFY = (70, 10)
    _P_INSTALL = (80, 15)
    _P_CLEANUP = (95, 5)

    def __init__(self, resources_url, download_list, delete_list,
                 resource_root='/home/rpdzkj/resources', retry=3,
                 manifest_hash=None, parent=None, manifest_bytes=None, resource_urls=None):
        """
        download_list 每项: {'path': ..., 'md5': ...}
            可选 'sha256'（优先于 md5）、'size'（字节，用于磁盘空间预检）
        manifest_hash: 可选，manifest.json 的 sha256，提供则会校验
        """
        super().__init__(parent)

        self.resources_url = resources_url.rstrip('/')
        self.resource_urls = resource_urls or [self.resources_url]
        self.download_list = download_list
        self.delete_list = delete_list
        self.resource_root = os.path.realpath(resource_root)
        self.retry = max(int(retry), 1)
        self.manifest_bytes = manifest_bytes
        self.manifest_hash = manifest_hash

        self.temp_dir = None
        self.download_dir = None
        self.backup_dir = None
        self.replaced_dir = None      # 被覆盖文件的备份
        self.deleted_dir = None       # 被删除文件的备份
        self.manifest_backup = None

        # 回滚日志
        self.created = []             # 本次新建的文件（回滚时删除）
        self.manifest_created = False

        self._stop_flag = False

    # ===================== QThread 入口 =====================

    def run(self):
        logger.info('resources update started')
        ok, msg = False, ''

        try:
            self._prepare()
            self._validate()

            self.statusChanged.emit('Downloading')
            self.download_all()
            self._check_stop()

            self.statusChanged.emit('Verifying')
            self.verify_all()
            self._check_stop()

            self.statusChanged.emit('Installing')
            self.install_all()
            self._check_stop()

            self.statusChanged.emit('Cleanup')
            self.cleanup()

            self.progressChanged.emit(100)
            ok = True

        except UpdateCancelled:
            logger.info('update cancelled')
            msg = 'update cancelled' + self._safe_rollback()

        except Exception as e:
            logger.exception('update failed')
            msg = str(e) + self._safe_rollback()

        finally:
            # 先清理临时目录（备份也在里面，所以回滚必须在此之前完成）
            self._remove_temp()
            self.result_ready.emit(ok, msg)


    # ===================== 取消 =====================

    def stop(self):
        self._stop_flag = True

    def _check_stop(self):
        if self._stop_flag or self.isInterruptionRequested():
            raise UpdateCancelled('update cancelled')

    def _sleep(self, seconds):
        """可被取消打断的 sleep。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check_stop()
            time.sleep(0.1)

    # ===================== 工具 =====================

    @staticmethod
    def _hash(filename, algo='md5'):
        h = hashlib.new(algo)
        with open(filename, 'rb') as f:
            for block in iter(lambda: f.read(1024 * 1024), b''):
                h.update(block)
        return h.hexdigest()

    @staticmethod
    def _safe(base, rel):
        """把服务器给的相对路径解析到 base 之下，防止 ../ 越界。"""
        base = os.path.realpath(base)
        p = os.path.realpath(os.path.join(base, rel))
        if p == base or os.path.commonpath([p, base]) != base:
            raise RuntimeError(f'illegal path: {rel}')
        return p

    @staticmethod
    def _move(src, dst):
        """尽量原子地移动文件；跨文件系统时退化为 复制到临时名 + rename。"""
        try:
            os.replace(src, dst)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            tmp = dst + '.ota_tmp'
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            os.remove(src)

    def _prune_empty_dirs(self, d):
        """从 d 向上删除空目录，但不会删除 resource_root 本身。"""
        while d != self.resource_root and os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
            d = os.path.dirname(d)

    def _emit_progress(self, phase, done, total):
        start, span = phase
        self.progressChanged.emit(int(start + span * done / max(total, 1)))

    def _prepare(self):
        # 临时目录放在 resource_root 同级，保证与目标同一文件系统，rename 才是原子的
        parent = os.path.dirname(self.resource_root)
        try:
            self.temp_dir = tempfile.mkdtemp(prefix='.resource_ota_', dir=parent)
        except OSError:
            logger.warning('cannot create temp dir next to resource_root, '
                           'falling back to system temp')
            self.temp_dir = tempfile.mkdtemp(prefix='resource_ota_')

        self.download_dir = os.path.join(self.temp_dir, 'download')
        self.backup_dir = os.path.join(self.temp_dir, 'backup')
        self.replaced_dir = os.path.join(self.backup_dir, 'replaced')
        self.deleted_dir = os.path.join(self.backup_dir, 'deleted')
        self.manifest_backup = os.path.join(self.backup_dir, 'manifest.json')

        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(self.replaced_dir, exist_ok=True)
        os.makedirs(self.deleted_dir, exist_ok=True)

        # 磁盘空间预检（需要下载 + 备份，约 2 倍）
        total = sum(int(i.get('size', 0)) for i in self.download_list)
        if total:
            free = shutil.disk_usage(self.temp_dir).free
            if free < total * 2:
                raise RuntimeError(
                    f'not enough disk space: need {total * 2}, free {free}')

    def _validate(self):
        """提前校验所有来自服务器的路径和字段。"""
        for item in self.download_list:
            if 'path' not in item or not ('md5' in item or 'sha256' in item):
                raise RuntimeError(f'invalid download item: {item}')
            self._safe(self.download_dir, item['path'])
            self._safe(self.resource_root, item['path'])
        for rel in self.delete_list:
            self._safe(self.resource_root, rel)

    def _remove_temp(self):
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ===================== 下载 =====================

    def _download_file(self, session, url, dst, timeout=60):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        last = None

        for attempt in range(self.retry):
            try:
                with session.get(url, stream=True, timeout=timeout) as r:
                    r.raise_for_status()
                    with open(dst, 'wb') as f:
                        for chunk in r.iter_content(1024 * 1024):
                            self._check_stop()
                            if chunk:
                                f.write(chunk)
                return

            except UpdateCancelled:
                raise

            except Exception as e:
                last = e
                logger.warning('download %s failed (%d/%d): %s',
                               url, attempt + 1, self.retry, e)
                if attempt < self.retry - 1:
                    self._sleep(2 ** attempt)   # 1s, 2s, 4s ...

        raise last

    def download_all(self):
        total = len(self.download_list)

        with requests.Session() as session:
            for idx, item in enumerate(self.download_list):
                self._check_stop()

                url = f'{self.resources_url}/{item["path"]}'
                dst = self._safe(self.download_dir, item['path'])
                logger.info('downloading %s', item['path'])

                from urllib.parse import quote
                last = None
                for base in self.resource_urls:
                    self._check_stop()
                    try:
                        self._download_file(session, base.rstrip('/') + '/' + quote(item['path'], safe='/'), dst)
                        algo = 'sha256' if 'sha256' in item else 'md5'
                        if self._hash(dst, algo) != item[algo]:
                            raise RuntimeError('resource hash mismatch: ' + item['path'])
                        break
                    except UpdateCancelled:
                        raise
                    except Exception as e:
                        last = e
                        logger.warning('resource source failed %s: %s', base, e)
                else:
                    raise last or RuntimeError('no resource download source')
                self._emit_progress(self._P_DOWNLOAD, idx + 1, total)

            # 下载 manifest
            self._check_stop()
            manifest_path = os.path.join(self.temp_dir, 'manifest.json')
            if self.manifest_bytes is not None:
                with open(manifest_path, 'wb') as f:
                    f.write(self.manifest_bytes)
            else:
                self._download_file(session, f'{self.resources_url}/manifest.json', manifest_path, timeout=30)

        self._emit_progress(self._P_DOWNLOAD, 1, 1)

    # ===================== 校验 =====================

    def verify_all(self):
        total = len(self.download_list)

        for idx, item in enumerate(self.download_list):
            self._check_stop()

            f = self._safe(self.download_dir, item['path'])

            if 'sha256' in item:
                ok = self._hash(f, 'sha256') == item['sha256']
            else:
                ok = self._hash(f, 'md5') == item['md5']

            if not ok:
                raise RuntimeError(f'{item["path"]} hash mismatch')

            self._emit_progress(self._P_VERIFY, idx + 1, total)

        if self.manifest_hash:
            m = os.path.join(self.temp_dir, 'manifest.json')
            if self._hash(m, 'sha256') != self.manifest_hash:
                raise RuntimeError('manifest.json hash mismatch')

        self._emit_progress(self._P_VERIFY, 1, 1)

    # ===================== 安装 =====================

    def install_all(self):
        total = len(self.download_list)

        for idx, item in enumerate(self.download_list):
            self._check_stop()

            src = self._safe(self.download_dir, item['path'])
            dst = self._safe(self.resource_root, item['path'])

            os.makedirs(os.path.dirname(dst), exist_ok=True)

            if os.path.exists(dst):
                bk = os.path.join(self.replaced_dir, item['path'])
                os.makedirs(os.path.dirname(bk), exist_ok=True)
                shutil.copy2(dst, bk)
            else:
                self.created.append(dst)    # 先记日志再替换，失败也能回滚

            self._move(src, dst)
            self._emit_progress(self._P_INSTALL, idx + 1, total)

        # 替换 manifest（最后一步），同样先备份
        src_manifest = os.path.join(self.temp_dir, 'manifest.json')
        dst_manifest = os.path.join(self.resource_root, 'manifest.json')

        if os.path.exists(dst_manifest):
            shutil.copy2(dst_manifest, self.manifest_backup)
        else:
            self.manifest_created = True

        self._move(src_manifest, dst_manifest)
        self._emit_progress(self._P_INSTALL, 1, 1)

    # ===================== 清理 =====================

    def cleanup(self):
        total = len(self.delete_list)

        for idx, rel in enumerate(self.delete_list):
            self._check_stop()

            p = self._safe(self.resource_root, rel)

            if os.path.isfile(p):
                # 不直接删除，而是移入备份目录，失败时可恢复
                bk = os.path.join(self.deleted_dir, rel)
                os.makedirs(os.path.dirname(bk), exist_ok=True)
                self._move(p, bk)

            self._prune_empty_dirs(os.path.dirname(p))
            self._emit_progress(self._P_CLEANUP, idx + 1, total)

    # ===================== 回滚 =====================

    def _safe_rollback(self):
        """回滚永远不向外抛异常；返回要追加到错误信息的文字。"""
        try:
            errors = self.rollback()
        except Exception as e:
            logger.exception('rollback crashed')
            return f' (rollback failed: {e})'

        if errors:
            return f' (rollback incomplete: {len(errors)} error(s))'
        return ''

    def rollback(self):
        errors = []
        if not self.backup_dir or not os.path.isdir(self.backup_dir):
            return errors

        # 1. 删除本次新建的文件
        for p in reversed(self.created):
            try:
                if os.path.isfile(p):
                    os.remove(p)
                self._prune_empty_dirs(os.path.dirname(p))
            except Exception as e:
                logger.error('rollback remove %s failed: %s', p, e)
                errors.append(e)

        # 2. 恢复被覆盖的文件 和 被删除的文件
        for base in (self.replaced_dir, self.deleted_dir):
            for root, _, files in os.walk(base):
                for name in files:
                    src = os.path.join(root, name)
                    rel = os.path.relpath(src, base)
                    dst = os.path.join(self.resource_root, rel)
                    try:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy2(src, dst)
                    except Exception as e:
                        logger.error('rollback restore %s failed: %s', rel, e)
                        errors.append(e)

        # 3. 恢复 manifest
        dst_manifest = os.path.join(self.resource_root, 'manifest.json')
        try:
            if os.path.isfile(self.manifest_backup):
                shutil.copy2(self.manifest_backup, dst_manifest)
            elif self.manifest_created and os.path.isfile(dst_manifest):
                os.remove(dst_manifest)
        except Exception as e:
            logger.error('rollback manifest failed: %s', e)
            errors.append(e)

        return errors