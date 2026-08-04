"""Secure global API-key storage and local environment resolution.

Global values use the macOS Keychain rather than a plaintext project or shell
file. User-facing messages identify credentials only as ``$NAME``.
"""

import getpass
import os
import shutil
import subprocess
import sys


SUPPORTED = ("UPSTAGE_API_KEY", "TINYFISH_API_KEY")
KEYCHAIN_PREFIX = "solar-code"


class CredentialError(RuntimeError):
    pass


def display_name(name: str) -> str:
    _validate_name(name)
    return f"${name}"


def _validate_name(name: str) -> None:
    if name not in SUPPORTED:
        raise CredentialError(f"지원하지 않는 credential 이름입니다: {name}")


def _service(name: str) -> str:
    return f"{KEYCHAIN_PREFIX}:{name}"


def keychain_available() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def load_keychain(name: str) -> str:
    """Read one value from the user's macOS Keychain without printing it."""
    _validate_name(name)
    if not keychain_available():
        return ""
    result = subprocess.run(
        ["security", "find-generic-password", "-a", getpass.getuser(),
         "-s", _service(name), "-w"],
        capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def save_global(name: str, value: str) -> str:
    """Store one value in the macOS Keychain and return its display marker."""
    _validate_name(name)
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise CredentialError(f"{display_name(name)} 값이 비어 있거나 잘못되었습니다.")
    if not keychain_available():
        raise CredentialError(
            "전역 보안 저장은 현재 macOS Keychain에서만 지원합니다. "
            f"이 환경에서는 {display_name(name)} 환경변수를 사용하세요.")
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", getpass.getuser(),
         "-s", _service(name), "-w"],
        input=value + "\n", capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise CredentialError(f"{display_name(name)} Keychain 저장에 실패했습니다.")
    os.environ[name] = value
    return display_name(name)


def _load_env_file(path: str, name: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith(name + "="):
                continue
            value = line.split("=", 1)[1].strip().strip("'\"")
            if value in (f"${name}", f"${{{name}}}"):
                return os.environ.get(name, "").strip() or load_keychain(name)
            return value
    return ""


def load(name: str, package_root: str | None = None) -> str:
    """Resolve environment, project ``.env``, Keychain, then package ``.env``."""
    _validate_name(name)
    value = os.environ.get(name, "").strip()
    if value:
        return value
    value = _load_env_file(os.path.join(os.getcwd(), ".env"), name)
    if value:
        return value
    value = load_keychain(name)
    if value:
        os.environ[name] = value
        return value
    if package_root:
        return _load_env_file(os.path.join(package_root, ".env"), name)
    return ""
