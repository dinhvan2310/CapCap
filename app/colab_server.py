"""Colab-hosted CapCap API.

The Colab deployment keeps the active media working set on local ephemeral
disk and mirrors project metadata to a durable Drive directory.  It exposes a
small file-ID based API so the browser never sends Windows paths to Colab.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import queue
import secrets
import shutil
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


APP_ROOT = Path(__file__).resolve().parent.parent
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def _env_path(name: str, default: str) -> Path:
    value = str(os.getenv(name, default) or default).strip()
    return Path(value).expanduser().resolve()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_name(value: str, fallback: str = "upload.bin") -> str:
    name = Path(str(value or "")).name.strip().replace("\x00", "")
    if not name or name in {".", ".."}:
        return fallback
    return name[:180]


class ColabStorage:
    def __init__(self) -> None:
        default_work = "/content/capcap" if os.name != "nt" else str(APP_ROOT / "temp" / "colab")
        self.work_root = _env_path("CAPCAP_COLAB_WORK_ROOT", default_work)
        self.drive_root = _env_path(
            "CAPCAP_DRIVE_ROOT",
            "/content/drive/MyDrive/CapCap" if os.name != "nt" else str(APP_ROOT / "projects"),
        )
        self.upload_root = self.work_root / "uploads"
        self.project_root = self.work_root / "projects"
        self.drive_project_root = self.drive_root / "projects"
        for path in (self.upload_root, self.project_root, self.drive_project_root):
            path.mkdir(parents=True, exist_ok=True)

    def _project_id(self, filename: str, fingerprint: str) -> str:
        stem = Path(filename).stem or "project"
        slug = "".join(char.lower() if char.isalnum() else "_" for char in stem).strip("_") or "project"
        return f"{slug}_{fingerprint[:12]}"

    def init_upload(self, filename: str, size: int, fingerprint: str = "") -> dict[str, Any]:
        if size < 0:
            raise ValueError("size must be non-negative")
        max_size = int(os.getenv("CAPCAP_MAX_UPLOAD_BYTES", str(100 * 1024**3)))
        if size > max_size:
            raise ValueError(f"upload exceeds CAPCAP_MAX_UPLOAD_BYTES ({max_size} bytes)")
        upload_id = uuid.uuid4().hex
        upload_dir = self.upload_root / upload_id
        upload_dir.mkdir(parents=True, exist_ok=False)
        meta = {"upload_id": upload_id, "filename": _safe_name(filename), "size": int(size), "fingerprint": fingerprint}
        (upload_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (upload_dir / "data.part").touch()
        return {**meta, "received": 0}

    def _upload(self, upload_id: str) -> tuple[Path, dict[str, Any]]:
        upload_dir = self.upload_root / str(upload_id)
        meta_path = upload_dir / "meta.json"
        data_path = upload_dir / "data.part"
        if not meta_path.exists() or not data_path.exists():
            raise FileNotFoundError("upload not found")
        return data_path, json.loads(meta_path.read_text(encoding="utf-8"))

    def write_chunk(self, upload_id: str, offset: int, body: bytes) -> dict[str, Any]:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        data_path, meta = self._upload(upload_id)
        expected = int(meta["size"])
        if offset > expected or offset + len(body) > expected:
            raise ValueError("chunk exceeds declared upload size")
        with data_path.open("r+b") as handle:
            handle.seek(offset)
            handle.write(body)
        received = min(expected, data_path.stat().st_size)
        return {"upload_id": upload_id, "received": received, "size": expected, "complete": received == expected}

    def complete_upload(self, upload_id: str) -> dict[str, Any]:
        data_path, meta = self._upload(upload_id)
        expected = int(meta["size"])
        if data_path.stat().st_size != expected:
            raise ValueError(f"upload incomplete: received {data_path.stat().st_size}/{expected}")
        digest = hashlib.sha256()
        with data_path.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
        fingerprint = digest.hexdigest()
        requested = str(meta.get("fingerprint") or "").strip().lower()
        if requested and not hmac.compare_digest(requested, fingerprint):
            raise ValueError("uploaded fingerprint does not match the content")
        project_id = self._project_id(meta["filename"], fingerprint)
        active_dir = self.project_root / project_id
        source_dir = active_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        input_path = source_dir / _safe_name(meta["filename"])
        shutil.copyfile(data_path, input_path)
        project = self._restore_or_create_project(project_id, input_path, meta["filename"], fingerprint)
        shutil.rmtree(self.upload_root / upload_id, ignore_errors=True)
        return project

    def _restore_or_create_project(self, project_id: str, input_path: Path, filename: str, fingerprint: str) -> dict[str, Any]:
        active_dir = self.project_root / project_id
        active_state_path = active_dir / "project.json"
        durable_state_path = self.drive_project_root / project_id / "project.json"
        active_was_present = active_state_path.exists()
        state: dict[str, Any] = {}
        resumed = False
        if durable_state_path.exists():
            try:
                state = json.loads(durable_state_path.read_text(encoding="utf-8"))
                resumed = True
            except (OSError, ValueError):
                state = {}
        state.update({
            "project_id": project_id,
            "project_root": str(active_dir),
            "input_video": str(input_path),
            "input_video_name": filename,
            "input_fingerprint": fingerprint,
            "updated_at": _utc_now(),
            "resume": {
                "matched": resumed,
                # A Drive match after a Colab reset has metadata but not the
                # old /content artifact files.  A same-session re-upload can
                # keep using its active artifacts.
                "requires_rebuild": bool(resumed and not active_was_present),
            },
        })
        state.setdefault("created_at", _utc_now())
        state.setdefault("steps", {})
        state.setdefault("settings", {})
        state.setdefault("artifacts", {})
        active_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        durable_dir = self.drive_project_root / project_id
        durable_dir.mkdir(parents=True, exist_ok=True)
        durable_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "project_id": project_id,
            "filename": filename,
            "fingerprint": fingerprint,
            "project_root": str(active_dir),
            "input_video": str(input_path),
            "resumed": resumed,
            "requires_rebuild": bool(resumed and not active_was_present),
            "state": state,
        }

    def load_project(self, project_id: str) -> dict[str, Any]:
        path = self.project_root / str(project_id) / "project.json"
        if not path.exists():
            raise FileNotFoundError("project not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_project(self, project_id: str, state: dict[str, Any]) -> dict[str, Any]:
        active_dir = self.project_root / str(project_id)
        if not active_dir.exists():
            raise FileNotFoundError("project not found")
        state = dict(state)
        state["project_id"] = str(project_id)
        state["project_root"] = str(active_dir)
        state["updated_at"] = _utc_now()
        active_path = active_dir / "project.json"
        active_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        durable_dir = self.drive_project_root / str(project_id)
        durable_dir.mkdir(parents=True, exist_ok=True)
        (durable_dir / "project.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return state

    def artifact_path(self, project_id: str, artifact_name: str) -> Path:
        state = self.load_project(project_id)
        raw = str(state.get("artifacts", {}).get(artifact_name, "") or "")
        candidate = Path(raw)
        project_dir = (self.project_root / str(project_id)).resolve()
        if not raw:
            raise FileNotFoundError("artifact not found")
        if not candidate.is_absolute():
            candidate = project_dir / candidate
        candidate = candidate.resolve()
        if project_dir not in candidate.parents and candidate != project_dir:
            raise PermissionError("artifact path escapes project root")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError("artifact not found")
        return candidate


@dataclass
class Job:
    job_id: str
    project_id: str
    phase: str
    payload: dict[str, Any]
    status: str = "queued"
    progress: int = 0
    message: str = "Queued"
    error: str = ""
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "phase": self.phase,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobManager:
    def __init__(self, storage: ColabStorage):
        self.storage = storage
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.active_job_id = ""

    def get(self, job_id: str) -> Job:
        with self.lock:
            job = self.jobs.get(str(job_id))
        if not job:
            raise KeyError("job not found")
        return job

    def _emit(self, job: Job, message: str, progress: int | None = None, status: str | None = None) -> None:
        with self.lock:
            if progress is not None:
                job.progress = max(0, min(100, int(progress)))
            if status:
                job.status = status
            job.message = str(message or job.message)
            job.updated_at = _utc_now()
            job.events.append({"type": "progress", **job.snapshot()})

    def start(self, project_id: str, phase: str, payload: dict[str, Any]) -> Job:
        with self.lock:
            if self.active_job_id:
                active = self.jobs.get(self.active_job_id)
                if active and active.status in {"queued", "running", "cancelling"}:
                    raise RuntimeError(f"session is busy with job {active.job_id}")
            job = Job(uuid.uuid4().hex, str(project_id), str(phase or "prepare"), dict(payload or {}))
            self.jobs[job.job_id] = job
            self.active_job_id = job.job_id
            job.events.append({"type": "progress", **job.snapshot()})
        thread = threading.Thread(target=self._run, args=(job,), name=f"capcap-job-{job.job_id[:8]}", daemon=True)
        thread.start()
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self.lock:
            if job.status in {"queued", "running"}:
                job.cancel_event.set()
                self._emit(job, "Cancellation requested", status="cancelling")
        return job

    def retry(self, job_id: str) -> Job:
        previous = self.get(job_id)
        with self.lock:
            if previous.status not in {"failed", "cancelled"}:
                raise RuntimeError("only failed or cancelled jobs can be retried")
        return self.start(previous.project_id, previous.phase, previous.payload)

    def _check_cancel(self, job: Job) -> None:
        if job.cancel_event.is_set():
            raise InterruptedError("job cancelled")

    def _callback(self, job: Job, phase: str) -> Callable[..., None]:
        labels = {
            "prepare": (5, "Preparing project"),
            "extract_audio": (20, "Extracting audio"),
            "extraction": (20, "Extracting audio"),
            "separation": (35, "Separating audio"),
            "diarization": (45, "Detecting speakers"),
            "transcription": (60, "Transcribing audio"),
            "translation": (78, "Translating subtitles"),
            "voice": (86, "Generating voice"),
            "export": (95, "Exporting video"),
        }

        def callback(step: Any = "processing", *args: Any, **kwargs: Any) -> None:
            self._check_cancel(job)
            progress, label = labels.get(str(step), (max(1, job.progress), str(step or phase)))
            self._emit(job, label, progress=progress)

        return callback

    def _run(self, job: Job) -> None:
        try:
            self._emit(job, "Starting", progress=1, status="running")
            state = self.storage.load_project(job.project_id)
            project_root = str(state["project_root"])
            input_video = str(state["input_video"])
            from services import WorkflowRuntime

            runtime = WorkflowRuntime(str(self.storage.work_root))
            phase = job.phase.lower().replace(" ", "_")
            if phase in {"prepare", "run_all"}:
                self._emit(job, "Preparing project", progress=5)
                previous_project_id = os.environ.get("CAPCAP_PROJECT_ID")
                os.environ["CAPCAP_PROJECT_ID"] = job.project_id
                try:
                    runtime.run_prepare(
                        input_video,
                        source_language=str(job.payload.get("source_language", state.get("input_language", "auto")) or "auto"),
                        target_language=str(job.payload.get("target_language", state.get("target_language", "vi")) or "vi"),
                        mode=str(job.payload.get("mode", state.get("mode", "subtitle")) or "subtitle"),
                        audio_handling_mode=str(job.payload.get("audio_handling_mode", "fast") or "fast"),
                        translator_ai=bool(job.payload.get("translator_ai", state.get("translator_ai", True))),
                        translator_style=str(job.payload.get("translator_style", state.get("translator_style", "")) or ""),
                        whisper_model_name=str(job.payload.get("whisper_model_name", "base") or "base"),
                        transcription_engine=str(job.payload.get("transcription_engine", "whisper") or "whisper"),
                        speaker_diarization=bool(job.payload.get("speaker_diarization", False)),
                        skip_translation=bool(job.payload.get("skip_translation", False)),
                        step_callback=self._callback(job, "prepare"),
                    )
                finally:
                    if previous_project_id is None:
                        os.environ.pop("CAPCAP_PROJECT_ID", None)
                    else:
                        os.environ["CAPCAP_PROJECT_ID"] = previous_project_id
                self._check_cancel(job)
                state = self.storage.load_project(job.project_id)
                self._mirror_state(job.project_id, state)
                if phase == "prepare":
                    self._finish(job, "Prepare complete")
                    return
            if phase in {"voice", "run_all"} and str(state.get("mode", job.payload.get("mode", "subtitle"))) in {"voice", "both"}:
                self._check_cancel(job)
                segments = self._load_segments(state)
                self._emit(job, "Generating voice", progress=82)
                result = runtime.run_voice(
                    segments=segments,
                    output_dir=str(Path(project_root) / "audio" / "tts_segments"),
                    background_path=str(job.payload.get("background_path", "") or ""),
                    audio_handling_mode=str(job.payload.get("audio_handling_mode", "fast") or "fast"),
                    voice_name=str(job.payload.get("voice_name", "ngochuyen") or "ngochuyen"),
                    voice_speed=float(job.payload.get("voice_speed", 1.0) or 1.0),
                    timing_sync_mode=str(job.payload.get("timing_sync_mode", "off") or "off"),
                    original_volume=int(job.payload.get("original_volume", 50) or 50),
                    dub_volume=int(job.payload.get("dub_volume", 100) or 100),
                    project_state_path=str(Path(project_root) / "project.json"),
                    project_temp_dir=str(Path(project_root) / "preview" / "cache"),
                    source_language=str(job.payload.get("source_language", "auto") or "auto"),
                    on_progress=self._callback(job, "voice"),
                )
                if isinstance(result, dict):
                    state.setdefault("artifacts", {}).update({k: v for k, v in result.items() if isinstance(v, str)})
                self.storage.save_project(job.project_id, state)
                state = self.storage.load_project(job.project_id)
            if phase in {"export", "run_all"}:
                self._check_cancel(job)
                self._emit(job, "Exporting video", progress=92)
                output_path = str(job.payload.get("output_path") or Path(project_root) / "export" / f"{job.project_id}.mp4")
                artifacts = dict(state.get("artifacts", {}) or {})
                runtime.run_export(
                    video_path=input_video,
                    output_path=output_path,
                    mode=str(job.payload.get("mode", state.get("mode", "subtitle")) or "subtitle"),
                    srt_path=str(job.payload.get("srt_path", artifacts.get("subtitle_translated_srt", "")) or ""),
                    ass_path=str(job.payload.get("ass_path", "") or ""),
                    audio_path=str(job.payload.get("audio_path", artifacts.get("mixed_audio", artifacts.get("voice_track", artifacts.get("voice_vi", "")))) or ""),
                    subtitle_style=dict(job.payload.get("subtitle_style", {}) or {}),
                    output_quality=str(job.payload.get("output_quality", "source") or "source"),
                    output_fps=str(job.payload.get("output_fps", "source") or "source"),
                    output_ratio=str(job.payload.get("output_ratio", "source") or "source"),
                    output_scale_mode=str(job.payload.get("output_scale_mode", "fit") or "fit"),
                    project_state_path=str(Path(project_root) / "project.json"),
                    project_temp_dir=str(Path(project_root) / "preview" / "cache"),
                    on_progress=self._callback(job, "export"),
                )
                state.setdefault("artifacts", {})["final_video"] = output_path
                self.storage.save_project(job.project_id, state)
            self._finish(job, "Job complete")
        except InterruptedError as exc:
            self._emit(job, str(exc), progress=job.progress, status="cancelled")
        except Exception as exc:  # workflow errors are user-visible job failures
            self._emit(job, str(exc), progress=job.progress, status="failed")
            with self.lock:
                job.error = str(exc)
                job.events.append({"type": "error", **job.snapshot()})
        finally:
            with self.lock:
                if self.active_job_id == job.job_id:
                    self.active_job_id = ""

    def _finish(self, job: Job, message: str) -> None:
        self._emit(job, message, progress=100, status="completed")

    def _mirror_state(self, project_id: str, state: dict[str, Any]) -> None:
        try:
            self.storage.save_project(project_id, state)
        except Exception:
            pass

    @staticmethod
    def _load_segments(state: dict[str, Any]) -> list[dict[str, Any]]:
        artifacts = dict(state.get("artifacts", {}) or {})
        path = str(artifacts.get("translation_final", "") or artifacts.get("transcript_segments", ""))
        if not path or not os.path.exists(path):
            return []
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            return list(payload or [])
        except (OSError, ValueError, TypeError):
            return []


storage = ColabStorage()
jobs = JobManager(storage)
app = FastAPI(title="CapCap Colab", version="0.1.0")


def _expected_token() -> str:
    return str(os.getenv("CAPCAP_APP_TOKEN", "") or "").strip()


def _auth(authorization: str = Header(default=""), x_capcap_token: str = Header(default="")) -> None:
    expected = _expected_token()
    if not expected:
        raise HTTPException(status_code=503, detail="CAPCAP_APP_TOKEN is not configured")
    supplied = str(authorization or "").removeprefix("Bearer ").strip() or str(x_capcap_token or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid CapCap session token")


class UploadInit(BaseModel):
    filename: str = Field(min_length=1)
    size: int = Field(ge=0)
    fingerprint: str = ""


class ProjectSave(BaseModel):
    state: dict[str, Any]


class SegmentsSave(BaseModel):
    segments: list[dict[str, Any]]
    revision: int | None = None


class JobStart(BaseModel):
    phase: str = "prepare"
    payload: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "capcap-colab", "time": _utc_now()}


@app.get("/api/session")
def session(_: None = Depends(_auth)) -> dict[str, Any]:
    return {
        "ok": True,
        "service": "capcap-colab",
        "workspace": str(storage.work_root),
        "drive": str(storage.drive_root),
        "model": os.getenv("OPENAI_MODEL", "gemma4:31b-cloud"),
        "single_user": True,
    }


@app.post("/api/uploads/init")
def upload_init(body: UploadInit, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        return {"ok": True, **storage.init_upload(body.filename, body.size, body.fingerprint)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/uploads/{upload_id}/chunk")
async def upload_chunk(upload_id: str, request: Request, offset: int = 0, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        return {"ok": True, **storage.write_chunk(upload_id, offset, await request.body())}
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/uploads/{upload_id}/complete")
def upload_complete(upload_id: str, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        return {"ok": True, "project": storage.complete_upload(upload_id)}
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}")
def project_get(project_id: str, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        return {"ok": True, "state": storage.load_project(project_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/projects/{project_id}")
def project_save(project_id: str, body: ProjectSave, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        return {"ok": True, "state": storage.save_project(project_id, body.state)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}/segments")
def segments_get(project_id: str, kind: str = "translated", _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        state = storage.load_project(project_id)
        artifact_name = "translation_final" if str(kind).lower() != "transcript" else "transcript_segments"
        raw_path = str(dict(state.get("artifacts", {}) or {}).get(artifact_name, "") or "")
        if not raw_path or not Path(raw_path).exists():
            return {"ok": True, "segments": [], "revision": int(state.get("revision", 0) or 0)}
        payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        return {"ok": True, "segments": list(payload or []), "revision": int(state.get("revision", 0) or 0)}
    except (FileNotFoundError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/projects/{project_id}/segments")
def segments_save(project_id: str, body: SegmentsSave, _: None = Depends(_auth)) -> dict[str, Any]:
    if len(body.segments) > 100_000:
        raise HTTPException(status_code=413, detail="Too many subtitle segments")
    try:
        state = storage.load_project(project_id)
        current_revision = int(state.get("revision", 0) or 0)
        if body.revision is not None and int(body.revision) != current_revision:
            raise HTTPException(status_code=409, detail=f"Stale project revision; expected {current_revision}")
        normalized: list[dict[str, Any]] = []
        for item in body.segments:
            if not isinstance(item, dict):
                raise ValueError("Each segment must be an object")
            start = float(item.get("start", 0.0) or 0.0)
            end = float(item.get("end", start) or start)
            if start < 0 or end < start:
                raise ValueError("Invalid segment timing")
            edited = dict(item)
            edited["start"] = start
            edited["end"] = end
            edited["text"] = str(item.get("text", "") or "")
            normalized.append(edited)
        project_dir = storage.project_root / str(project_id)
        translation_dir = project_dir / "translation"
        subtitle_dir = project_dir / "subtitle"
        translation_dir.mkdir(parents=True, exist_ok=True)
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        json_path = translation_dir / "translation_final.json"
        srt_path = subtitle_dir / "subtitle_translated.srt"
        json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        from translation.srt_utils import to_srt
        srt_path.write_text(to_srt(normalized), encoding="utf-8")
        history = list(state.get("history", []) or [])
        history.append({"revision": current_revision, "segments": normalized})
        state["history"] = history[-200:]
        state["revision"] = current_revision + 1
        state.setdefault("artifacts", {})["translation_final"] = str(json_path)
        state["artifacts"]["subtitle_translated_srt"] = str(srt_path)
        state["steps"] = dict(state.get("steps", {}) or {})
        state["steps"]["translate_raw"] = "edited"
        saved = storage.save_project(project_id, state)
        return {"ok": True, "segments": normalized, "revision": int(saved["revision"])}
    except HTTPException:
        raise
    except (FileNotFoundError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/jobs")
def job_start(project_id: str, body: JobStart, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        storage.load_project(project_id)
        job = jobs.start(project_id, body.phase, body.payload)
        return {"ok": True, "job": job.snapshot()}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def job_get(job_id: str, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        return {"ok": True, "job": jobs.get(job_id).snapshot()}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        return {"ok": True, "job": jobs.cancel(job_id).snapshot()}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/retry")
def job_retry(job_id: str, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        return {"ok": True, "job": jobs.retry(job_id).snapshot()}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/rebuild")
def project_rebuild(project_id: str, body: JobStart, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        storage.load_project(project_id)
        job = jobs.start(project_id, body.phase or "prepare", body.payload)
        return {"ok": True, "job": job.snapshot()}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str, _: None = Depends(_auth)) -> StreamingResponse:
    try:
        job = jobs.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    def stream():
        cursor = 0
        while True:
            with jobs.lock:
                events = list(job.events)
                snapshot = job.snapshot()
            for event in events[cursor:]:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            cursor = len(events)
            if snapshot["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/projects/{project_id}/artifact/{artifact_name}")
def artifact(project_id: str, artifact_name: str, _: None = Depends(_auth)) -> FileResponse:
    try:
        path = storage.artifact_path(project_id, artifact_name)
    except (FileNotFoundError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream", filename=path.name)


@app.get("/api/projects/{project_id}/source")
def source_video(project_id: str, _: None = Depends(_auth)) -> FileResponse:
    try:
        state = storage.load_project(project_id)
        project_dir = (storage.project_root / str(project_id)).resolve()
        path = Path(str(state.get("input_video", "") or "")).resolve()
        if project_dir not in path.parents or not path.exists() or not path.is_file():
            raise FileNotFoundError("source video not found")
    except (FileNotFoundError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "video/mp4", filename=path.name)


@app.post("/api/projects/{project_id}/cleanup")
def project_cleanup(project_id: str, _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        project_dir = (storage.project_root / str(project_id)).resolve()
        storage.load_project(project_id)
        removed: list[str] = []
        for relative in ("analysis", "translation", "audio", "preview"):
            target = (project_dir / relative).resolve()
            if project_dir not in target.parents:
                continue
            if target.exists():
                shutil.rmtree(target)
                target.mkdir(parents=True, exist_ok=True)
                removed.append(relative)
        return {"ok": True, "removed": removed, "kept": ["source", "export", "project.json"]}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/save-to-drive")
def save_to_drive(project_id: str, artifact_name: str = "final_video", _: None = Depends(_auth)) -> dict[str, Any]:
    try:
        source = storage.artifact_path(project_id, artifact_name)
        destination_dir = storage.drive_project_root / str(project_id) / "exports"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name
        shutil.copyfile(source, destination)
        return {"ok": True, "artifact": artifact_name, "path": str(destination)}
    except (FileNotFoundError, PermissionError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


FRONTEND_DIST = APP_ROOT / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    if path.startswith("api/") or path == "health":
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"ok": False, "error": "Frontend dist is not built"}, status_code=503)


def main() -> None:
    import uvicorn

    host = os.getenv("CAPCAP_COLAB_HOST", "127.0.0.1")
    port = int(os.getenv("CAPCAP_COLAB_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("CAPCAP_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
