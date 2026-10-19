"""Current-user Windows DPAPI secrets. Config files contain references only."""
from __future__ import annotations

import ctypes
import os
import re
import uuid
from pathlib import Path


class SecretStore:
    def __init__(self, root: Path):
        self.directory = root / "harness" / "secrets"

    def _path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", name):
            raise ValueError("无效的密钥引用")
        return self.directory / f"{name}.bin"

    @staticmethod
    def _crypt(data: bytes, decrypt: bool = False) -> bytes:
        if os.name != "nt":
            raise RuntimeError("此平台请使用环境变量配置密钥")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        fn = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        fn.restype = wintypes.BOOL
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))

    def put(self, name: str, value: str) -> None:
        if not value.strip():
            return
        encrypted = self._crypt(value.strip().encode("utf-8"))
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_bytes(encrypted)
        temporary.replace(path)

    def get(self, name: str, env: str = "") -> str:
        path = self._path(name)
        if path.exists():
            # A damaged protected secret is an error, never silently a different identity.
            return self._crypt(path.read_bytes(), True).decode("utf-8")
        return os.environ.get(env, "") if env else ""
