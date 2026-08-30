from __future__ import annotations

LOCAL_PROFILE = "local"
def current_runtime_profile() -> str:
    return LOCAL_PROFILE


def is_remote_profile() -> bool:
    return False


def is_local_profile() -> bool:
    return current_runtime_profile() == LOCAL_PROFILE
