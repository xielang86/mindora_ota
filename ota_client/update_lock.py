"""Non-blocking Linux kernel lock. No service/process or third-party package."""
import fcntl
import os
from pathlib import Path


class UpdateLock:
    def __init__(self, path='/home/rpdzkj/update/update.lock'):
        self.path = Path(path)
        self.fd = None

    def acquire(self):
        if self.fd is not None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            os.close(self.fd)  # Last close releases flock. Never unlink the shared lock file.
            self.fd = None
