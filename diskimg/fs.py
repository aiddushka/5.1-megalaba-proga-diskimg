from dataclasses import dataclass

@dataclass
class fsent_t:
    name: str
    is_dir: bool
    size: int
    mtime: int

class FileSystem:
    type = "?"
    def list(self, path, cb): raise NotImplementedError
    def stat(self, path):     raise NotImplementedError
    def read(self, path, out): raise NotImplementedError
    def write(self, path, src): raise NotImplementedError
    def mkdir(self, path):    raise NotImplementedError
    def remove(self, path):   raise NotImplementedError
    def flush(self):          return 0
    def info(self, out):      pass
    def close(self):          pass