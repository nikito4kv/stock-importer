from __future__ import annotations

import base64
import ctypes
from ctypes import POINTER, byref, c_char
from ctypes import wintypes
import sys
from pathlib import Path


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", POINTER(c_char))]


def _blob_from_bytes(raw_bytes: bytes) -> tuple[_DataBlob, ctypes.Array[c_char]]:
    buffer = ctypes.create_string_buffer(raw_bytes, len(raw_bytes))
    blob = _DataBlob(len(raw_bytes), ctypes.cast(buffer, POINTER(c_char)))
    return blob, buffer


def _bytes_from_blob(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


class SecretStore:
    def __init__(self, root: str | Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self._root / f"{name}.secret"

    def _protect(self, secret_value: str) -> bytes:
        raw_bytes = secret_value.encode("utf-8")
        if sys.platform != "win32":
            return base64.b64encode(raw_bytes)

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = _blob_from_bytes(raw_bytes)
        output_blob = _DataBlob()
        success = crypt32.CryptProtectData(
            byref(input_blob),
            None,
            None,
            None,
            None,
            0,
            byref(output_blob),
        )
        del input_buffer
        if not success:
            raise RuntimeError("Unable to protect secret with DPAPI")
        try:
            return _bytes_from_blob(output_blob)
        finally:
            kernel32.LocalFree(output_blob.pbData)

    def _unprotect(self, raw_bytes: bytes) -> str | None:
        if sys.platform != "win32":
            return base64.b64decode(raw_bytes).decode("utf-8")

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = _blob_from_bytes(raw_bytes)
        output_blob = _DataBlob()
        success = crypt32.CryptUnprotectData(
            byref(input_blob),
            None,
            None,
            None,
            None,
            0,
            byref(output_blob),
        )
        del input_buffer
        if not success:
            return None
        try:
            return _bytes_from_blob(output_blob).decode("utf-8")
        finally:
            kernel32.LocalFree(output_blob.pbData)

    def set_secret(self, name: str, value: str) -> None:
        self._path(name).write_bytes(self._protect(value))

    def get_secret(self, name: str) -> str | None:
        path = self._path(name)
        if not path.exists() or not path.is_file():
            return None
        return self._unprotect(path.read_bytes())

    def delete_secret(self, name: str) -> None:
        path = self._path(name)
        if path.exists():
            path.unlink()
