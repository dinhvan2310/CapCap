import os
import shutil
import subprocess
import sys
from pathlib import Path


def bundle_root() -> str:
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        return os.path.abspath(str(meipass))
    if getattr(sys, "frozen", False):
        internal_dir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "_internal")
        if os.path.isdir(internal_dir):
            return internal_dir
    return str(Path(__file__).resolve().parents[1])


def workspace_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return str(Path(__file__).resolve().parents[1])


def join_root(*parts: str) -> str:
    return os.path.join(workspace_root(), *parts)


def asset_path(*parts: str) -> str:
    return first_existing_path(
        join_root("assets", *parts),
        os.path.join(bundle_root(), "assets", *parts),
    )


def app_path(*parts: str) -> str:
    return first_existing_path(
        join_root("app", *parts),
        os.path.join(bundle_root(), "app", *parts),
    )


def first_existing_path(*candidates: str) -> str:
    for candidate in candidates:
        path = str(candidate or "").strip()
        if path and os.path.exists(path):
            return path
    return str(candidates[0] if candidates else "")


def bin_path(*parts: str) -> str:
    primary = os.path.join(bundle_root(), "bin", *parts)
    workspace_fallback = join_root("bin", *parts)
    cwd_fallback = os.path.join(os.getcwd(), "bin", *parts)
    exe_fallback = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "bin", *parts)
    return first_existing_path(primary, workspace_fallback, cwd_fallback, exe_fallback)


def ffmpeg_binary_path() -> str:
    """Resolve FFmpeg on both the Windows bundle and Linux/Colab."""
    configured = str(os.getenv("CAPCAP_FFMPEG_PATH", "") or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    if os.name != "nt":
        return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    return bin_path("ffmpeg", "ffmpeg.exe")


def ffprobe_binary_path() -> str:
    """Resolve ffprobe beside the bundle or from the Linux PATH."""
    configured = str(os.getenv("CAPCAP_FFPROBE_PATH", "") or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    if os.name != "nt":
        return shutil.which("ffprobe") or "/usr/bin/ffprobe"
    return bin_path("ffmpeg", "ffprobe.exe")


_SUBTITLE_FONT_CACHE: dict[str, str] = {}


def resolve_subtitle_font_name(requested: str) -> str:
    """Return a font family available to libass on the current platform."""
    name = str(requested or "Arial").strip() or "Arial"
    if os.name == "nt":
        return name
    cached = _SUBTITLE_FONT_CACHE.get(name.casefold())
    if cached:
        return cached
    try:
        families_result = subprocess.run(
            ["fc-list", ":", "family"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        available = {
            family.strip().casefold()
            for line in families_result.stdout.splitlines()
            for family in line.split(",")
            if family.strip()
        }
    except (OSError, subprocess.SubprocessError):
        available = set()
    if name.casefold() in available:
        resolved = name
    else:
        resolved = next(
            (candidate for candidate in ("Roboto", "Noto Sans", "DejaVu Sans") if candidate.casefold() in available),
            "DejaVu Sans",
        )
    _SUBTITLE_FONT_CACHE[name.casefold()] = resolved
    return resolved


def models_path(*parts: str) -> str:
    writable = join_root("models", *parts)
    bundled = os.path.join(bundle_root(), "models", *parts)

    # Packaged builds create writable placeholder directories so optional
    # resources can be downloaded after installation.  For SenseVoice that
    # placeholder must not shadow a complete bundled model under _internal.
    # Keep the normal writable-first behavior for every other model family.
    if parts and str(parts[0]).strip().lower() == "sensevoice":
        required = ("model.int8.onnx", "tokens.txt")
        for candidate in (writable, bundled):
            if all(os.path.isfile(os.path.join(candidate, name)) for name in required):
                return candidate

    return first_existing_path(writable, bundled)


def temp_path(*parts: str) -> str:
    return join_root("temp", *parts)


def output_path(*parts: str) -> str:
    return join_root("output", *parts)


def subprocess_hidden_kwargs() -> dict:
    """Return Windows flags that keep console child processes invisible.

    The GUI build has no console of its own.  Without these flags, console
    programs such as FFmpeg/FFprobe create a temporary console window every
    time they start, which causes visible flashes and adds process-launch
    overhead.  The debug console build remains unaffected.
    """
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def subprocess_text_kwargs() -> dict:
    """Return safe options for text captured from external Windows tools.

    ``subprocess`` otherwise decodes captured output using the current Windows
    ANSI code page (often reported by Python as ``charmap``).  FFmpeg and
    FFprobe include the input path in diagnostics, so a Unicode file or user
    path can make that implicit decode fail before the actual workflow starts.
    Our bundled tools emit UTF-8 diagnostics; replacement is intentional for
    diagnostic text so an unexpected third-party byte never aborts a job.
    """
    return {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        **subprocess_hidden_kwargs(),
    }
