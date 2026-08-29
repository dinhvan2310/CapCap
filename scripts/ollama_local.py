"""Start and validate the local Ollama endpoint used by CapCap.

This module intentionally does not create a public proxy or a tunnel.  The
``gemma4:31b-cloud`` model is still executed by Ollama Cloud, while CapCap
talks to the local Ollama daemon at ``127.0.0.1:11434``.

Usage:
    python -m scripts.ollama_local --ensure
    python -m scripts.ollama_local --ensure --pull
    python -m scripts.ollama_local --test
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma4:31b-cloud"


def _load_project_env() -> None:
    """Load the project's simple KEY=VALUE environment file if present."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _base_url() -> str:
    configured = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
    if configured.lower().endswith("/v1"):
        configured = configured[:-3].rstrip("/")
    return configured or DEFAULT_BASE_URL


def _model() -> str:
    return os.getenv("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _ollama_command() -> str | None:
    command = shutil.which("ollama")
    if command:
        return command
    if sys.platform == "win32":
        candidates = (
            Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
            Path(os.getenv("ProgramFiles", "C:\\Program Files")) / "Ollama" / "ollama.exe",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


def _request(path: str, *, method: str = "GET", payload: dict | None = None, timeout: int = 5) -> dict:
    body = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{_base_url()}{path}", data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def is_ready() -> bool:
    try:
        _request("/api/tags")
        return True
    except (OSError, urllib.error.URLError, ValueError):
        return False


def start_server() -> None:
    if is_ready():
        return
    command = _ollama_command()
    if not command:
        raise RuntimeError(
            "Không tìm thấy Ollama. Hãy cài Ollama cho Windows và đảm bảo lệnh 'ollama' có trong PATH."
        )
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [command, "serve"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=sys.platform != "win32",
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if is_ready():
            return
        time.sleep(1)
    raise RuntimeError(f"Ollama chưa sẵn sàng tại {_base_url()}:11434")


def installed_models() -> set[str]:
    data = _request("/api/tags")
    return {str(item.get("name", "")) for item in data.get("models", []) if item.get("name")}


def ensure_model(*, pull: bool = False) -> None:
    model = _model()
    if model in installed_models():
        return
    if not pull:
        raise RuntimeError(
            f"Model '{model}' chưa có trong Ollama. Chạy: ollama signin && ollama pull {model}"
        )
    command = _ollama_command()
    if not command:
        raise RuntimeError("Không tìm thấy lệnh Ollama để pull model.")
    result = subprocess.run([command, "pull", model], check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Không pull được model '{model}'. Hãy chạy 'ollama signin' rồi thử lại.")


def test_chat() -> str:
    result = _request(
        "/v1/chat/completions",
        method="POST",
        payload={
            "model": _model(),
            "messages": [{"role": "user", "content": "Reply with exactly: CAPCAP_OLLAMA_LOCAL_OK"}],
            "stream": False,
        },
        timeout=300,
    )
    return str(result["choices"][0]["message"]["content"])


def main(argv: list[str] | None = None) -> int:
    _load_project_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ensure", action="store_true", help="Start Ollama if needed and verify the configured model.")
    parser.add_argument("--pull", action="store_true", help="Pull the configured model when it is missing.")
    parser.add_argument("--test", action="store_true", help="Send a small OpenAI-compatible chat request.")
    args = parser.parse_args(argv)
    try:
        if args.ensure or args.test:
            start_server()
            ensure_model(pull=args.pull)
            print(f"Ollama ready: {_base_url()} (model={_model()})")
        if args.test:
            print(test_chat())
        if not (args.ensure or args.test):
            parser.print_help()
        return 0
    except Exception as exc:
        print(f"[Ollama local] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
