"""Read this machine's Chrome cookies for goofish.com only.

Values stay in memory and are never logged. Other sites are not read.
"""

from datetime import datetime, timezone
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


CHROME_EPOCH = 11644473600
REQUIRED_FIELDS = ("unb", "cookie2", "_m_h5_tk", "sgcookie", "t", "cna")


class ChromeCookieError(Exception):
    """A local Chrome cookie could not be read."""


def _chrome_cookie_db() -> Path:
    path = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
    if not path.is_file():
        raise ChromeCookieError("未找到本机 Chrome 的默认配置，请先在 Chrome 登录闲鱼")
    return path


def _encryption_key() -> bytes:
    try:
        password = subprocess.check_output(
            ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChromeCookieError("无法读取 Chrome 钥匙串，请在系统提示中允许访问后重试") from exc
    if not password:
        raise ChromeCookieError("Chrome 钥匙串中没有可用的加密口令")
    return PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1003,
    ).derive(password.encode("utf-8"))


def _decrypt(blob: bytes, key: bytes) -> str:
    if not blob.startswith(b"v10") or (len(blob) - 3) % 16:
        raise ChromeCookieError("Chrome Cookie 加密格式不受支持")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    data = decryptor.update(blob[3:]) + decryptor.finalize()
    pad = data[-1]
    if not 1 <= pad <= 16 or data[-pad:] != bytes([pad]) * pad:
        raise ChromeCookieError("Chrome Cookie 解密失败，请确认钥匙串授权")
    # Current macOS Chrome stores a 32-byte hash before the cookie text.
    raw = data[:-pad]
    if len(raw) < 32:
        raise ChromeCookieError("Chrome Cookie 内容不完整")
    return raw[32:].decode("utf-8")


def _expired(has_expires: int, expires_utc: int) -> bool:
    if not has_expires or not expires_utc:
        return False
    expires_at = datetime.fromtimestamp(expires_utc / 1_000_000 - CHROME_EPOCH, timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def read_goofish_cookies() -> dict:
    """Return the cookie header and field names for the local Goofish login."""
    source = _chrome_cookie_db()
    key = _encryption_key()
    with tempfile.TemporaryDirectory(prefix="xianyu-chrome-") as directory:
        copied = Path(directory) / "Cookies"
        shutil.copy2(source, copied)
        connection = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                """
                SELECT name, encrypted_value, has_expires, expires_utc
                FROM cookies
                WHERE host_key = '.goofish.com'
                ORDER BY last_update_utc
                """
            ).fetchall()
        finally:
            connection.close()

    values = {}
    expired_names = []
    for name, blob, has_expires, expires_utc in rows:
        if not name or name in values:
            continue
        value = _decrypt(blob, key)
        if not value:
            continue
        if _expired(has_expires, expires_utc):
            expired_names.append(name)
        values[name] = value
    if not values.get("unb", "").isdigit():
        raise ChromeCookieError("Chrome 中没有有效的闲鱼登录态，请先在 Chrome 打开 goofish.com 并登录")
    return {
        "cookies": "; ".join(f"{name}={value}" for name, value in values.items()),
        "unb": values["unb"],
        "field_names": sorted(values),
        "missing_fields": [name for name in REQUIRED_FIELDS if name not in values],
        "expired_fields": sorted(expired_names),
    }
