import ctypes
import os
import sys
from ctypes import wintypes

from pydantic import SecretStr

CRED_TYPE_GENERIC = 1


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def target_name(user: str) -> str:
    return f"nc-autotag:{user}"


def _read_credential_manager(target: str) -> str | None:
    if sys.platform != "win32":
        return None
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_read = advapi.CredReadW
    cred_read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
    ]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi.CredFree
    cred_free.argtypes = [ctypes.c_void_p]

    ptr = ctypes.POINTER(_CREDENTIAL)()
    if not cred_read(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return blob.decode("utf-16-le")
    finally:
        cred_free(ptr)


def get_app_password(user: str) -> SecretStr | None:
    value = _read_credential_manager(target_name(user))
    if not value:
        value = os.environ.get(f"NC_AUTOTAG_PASSWORD_{user.upper()}")
    return SecretStr(value) if value else None
