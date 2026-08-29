import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type Project = {
  project_id: string;
  filename: string;
  fingerprint: string;
  resumed: boolean;
  requires_rebuild: boolean;
  state: Record<string, unknown>;
};

type Job = {
  job_id: string;
  project_id: string;
  phase: string;
  status: string;
  progress: number;
  message: string;
  error?: string;
};

type Segment = { start: number; end: number; text: string; source_text?: string; [key: string]: unknown };

const savedToken = sessionStorage.getItem("capcap-session-token") ?? "";

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(init?.headers ?? {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(String(payload.detail ?? payload.error ?? response.statusText));
  return payload as T;
}

function App() {
  const [token, setToken] = useState(savedToken);
  const [connected, setConnected] = useState(false);
  const [session, setSession] = useState<Record<string, unknown> | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [phase, setPhase] = useState("prepare");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [logs, setLogs] = useState<string[]>([]);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [sourceUrl, setSourceUrl] = useState("");
  const [previewError, setPreviewError] = useState("");
  const [segments, setSegments] = useState<Segment[]>([]);
  const [revision, setRevision] = useState(0);
  const fileInput = useRef<HTMLInputElement>(null);

  const connect = useCallback(async () => {
    const value = token.trim();
    if (!value) return setMessage("Dán session token trước khi kết nối.");
    try {
      const response = await request<Record<string, unknown>>("/api/session", value);
      sessionStorage.setItem("capcap-session-token", value);
      setToken(value);
      setSession(response);
      setConnected(true);
      setMessage("Đã kết nối Colab server.");
    } catch (error) {
      setConnected(false);
      setMessage(error instanceof Error ? error.message : "Không kết nối được server.");
    }
  }, [token]);

  useEffect(() => {
    if (savedToken) void connect();
  }, [connect]);

  useEffect(() => {
    let objectUrl = "";
    if (!project || !token) { setSourceUrl(""); setPreviewError(""); return; }
    setPreviewError("");
    void fetch(`/api/projects/${project.project_id}/source`, { headers: { Authorization: `Bearer ${token}` } })
      .then(async (response) => {
        if (response.ok) return response.blob();
        const detail = await response.json().catch(() => ({})) as { detail?: string };
        throw new Error(detail.detail ?? `Preview request failed (${response.status})`);
      })
      .then((blob) => { objectUrl = URL.createObjectURL(blob); setSourceUrl(objectUrl); })
      .catch((error: unknown) => { setSourceUrl(""); setPreviewError(error instanceof Error ? error.message : "Preview request failed"); });
    return () => { if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [project, token]);

  useEffect(() => {
    if (!project || !token) { setSegments([]); return; }
    void request<{ segments: Segment[]; revision: number }>(`/api/projects/${project.project_id}/segments`, token)
      .then((result) => { setSegments(result.segments); setRevision(result.revision); })
      .catch(() => setSegments([]));
  }, [project, token]);

  const upload = async (file: File) => {
    setBusy(true); setMessage("Đang khởi tạo upload..."); setUploadProgress(0);
    try {
      const init = await request<{ upload_id: string; size: number }>("/api/uploads/init", token, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, size: file.size }),
      });
      const chunkSize = 8 * 1024 * 1024;
      for (let offset = 0; offset < file.size; offset += chunkSize) {
        const chunk = file.slice(offset, Math.min(file.size, offset + chunkSize));
        await request(`/api/uploads/${init.upload_id}/chunk?offset=${offset}`, token, {
          method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: chunk,
        });
        setUploadProgress(Math.round((Math.min(file.size, offset + chunk.size) / file.size) * 100));
      }
      const result = await request<{ project: Project }>(`/api/uploads/${init.upload_id}/complete`, token, { method: "POST" });
      setProject(result.project);
      setMessage(result.project.resumed ? "Đã khớp project cũ; cần kiểm tra/rebuild artifact." : "Upload hoàn tất.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Upload thất bại.");
    } finally { setBusy(false); }
  };

  const watchJob = async (jobId: string) => {
    const response = await fetch(`/api/jobs/${jobId}/events`, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok || !response.body) throw new Error("Không mở được progress stream.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder(); let buffer = "";
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      buffer += decoder.decode(next.value, { stream: true });
      const blocks = buffer.split("\n\n"); buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        const line = block.split("\n").find((item) => item.startsWith("data: "));
        if (!line) continue;
        const item = JSON.parse(line.slice(6)) as Job;
        setJob(item); setLogs((previous) => [...previous.slice(-79), `${item.progress}% · ${item.message}`]);
      }
    }
  };

  const run = async (selectedPhase: string) => {
    if (!project) return;
    setBusy(true); setLogs([]); setMessage(`Đang chạy ${selectedPhase}...`);
    try {
      const result = await request<{ job: Job }>(`/api/projects/${project.project_id}/jobs`, token, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phase: selectedPhase, payload: { mode: project.state.mode ?? "subtitle" } }),
      });
      setJob(result.job); await watchJob(result.job.job_id);
      setMessage("Job đã kết thúc.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Job thất bại."); }
    finally { setBusy(false); }
  };

  const cancel = async () => {
    if (!job) return;
    try { await request(`/api/jobs/${job.job_id}/cancel`, token, { method: "POST" }); setMessage("Đã yêu cầu hủy job."); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Không hủy được job."); }
  };

  const retry = async () => {
    if (!job) return;
    setBusy(true); setLogs([]);
    try {
      const result = await request<{ job: Job }>(`/api/jobs/${job.job_id}/retry`, token, { method: "POST" });
      setJob(result.job); await watchJob(result.job.job_id); setMessage("Retry đã kết thúc.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Retry thất bại."); }
    finally { setBusy(false); }
  };

  const rebuild = async () => {
    if (!project) return;
    setBusy(true); setLogs([]);
    try {
      const result = await request<{ job: Job }>(`/api/projects/${project.project_id}/rebuild`, token, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ phase: "prepare", payload: { mode: project.state.mode ?? "subtitle" } }),
      });
      setJob(result.job); await watchJob(result.job.job_id); setMessage("Rebuild đã kết thúc.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Rebuild thất bại."); }
    finally { setBusy(false); }
  };

  const cleanup = async () => {
    if (!project) return;
    try { await request(`/api/projects/${project.project_id}/cleanup`, token, { method: "POST" }); setMessage("Đã dọn artifact trung gian."); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Không dọn được artifact."); }
  };

  const saveExportToDrive = async () => {
    if (!project) return;
    try { await request(`/api/projects/${project.project_id}/save-to-drive?artifact_name=final_video`, token, { method: "POST" }); setMessage("Đã lưu export vào Google Drive."); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Chưa có export để lưu."); }
  };

  const saveSegments = async () => {
    if (!project) return;
    try {
      const result = await request<{ segments: Segment[]; revision: number }>(`/api/projects/${project.project_id}/segments`, token, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ segments, revision }),
      });
      setSegments(result.segments); setRevision(result.revision); setMessage("Đã autosave subtitle state.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Không lưu được subtitle."); }
  };

  const statusTone = useMemo(() => job?.status ?? (connected ? "ready" : "offline"), [connected, job]);
  return <div className="app-shell">
    <header className="topbar"><div><span className="eyebrow">CAPCAP / COLAB</span><h1>Subtitle workstation</h1></div><span className={`status status-${statusTone}`}>{statusTone}</span></header>
    <main className="workspace">
      {!connected && <section className="connect-panel panel"><div><span className="eyebrow">SESSION ACCESS</span><h2>Kết nối Colab</h2><p>Dán token được in bởi notebook. Token chỉ lưu trong sessionStorage của tab này.</p></div><div className="connect-row"><input value={token} onChange={(event) => setToken(event.target.value)} placeholder="CapCap session token" type="password" /><button onClick={() => void connect()}>Connect</button></div></section>}
      {connected && <>
        <section className="toolbar panel"><div><span className="eyebrow">WORKSPACE</span><h2>{project?.filename ?? "Chưa có video"}</h2><p>{String(session?.model ?? "model chưa xác định")} · one user / one active job</p></div><div className="actions"><input ref={fileInput} type="file" accept="video/*,.mkv,.webm,.mov,.mp4" hidden onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file); }} /><button disabled={busy} onClick={() => fileInput.current?.click()}>Upload video</button>{project && <><select value={phase} onChange={(event) => setPhase(event.target.value)} disabled={busy}><option value="prepare">Prepare</option><option value="voice">Voice</option><option value="export">Export</option></select><button disabled={busy} onClick={() => void run(phase)}>Run phase</button><button className="primary" disabled={busy} onClick={() => void run("run_all")}>Run all</button>{job && ["queued", "running", "cancelling"].includes(job.status) && <button className="danger" onClick={() => void cancel()}>Cancel</button>}{job && ["failed", "cancelled"].includes(job.status) && <><button disabled={busy} onClick={() => void retry()}>Retry</button><button disabled={busy} onClick={() => void rebuild()}>Rebuild phase</button></>}</>}</div></section>
        {uploadProgress > 0 && uploadProgress < 100 && <div className="progress-track"><span style={{ width: `${uploadProgress}%` }} /></div>}
        <section className="editor-grid"><div className="preview panel"><div className="panel-heading"><span>Preview</span><span className="muted">Browser playback</span></div>{sourceUrl ? <video className="source-video" src={sourceUrl} controls playsInline onError={() => setPreviewError("Browser không hỗ trợ codec/container của video này.")} /> : <div className="preview-empty">{project ? (previewError || "Đang tải preview…") : "Upload video để bắt đầu."}</div>}</div><div className="inspector panel"><div className="panel-heading"><span>Project</span><span className="muted">Revision {revision}</span></div>{project ? <><div className="stat"><span>Fingerprint</span><code>{project.fingerprint.slice(0, 20)}…</code></div><div className="stat"><span>Resume</span><strong>{project.resumed ? "Matched" : "New"}</strong></div><div className="stat"><span>Rebuild</span><strong>{project.requires_rebuild ? "Required" : "No"}</strong></div><button onClick={() => void saveSegments()} disabled={busy || !segments.length}>Save subtitle edits</button><button onClick={() => void saveExportToDrive()} disabled={busy}>Save export to Drive</button><button onClick={() => void cleanup()} disabled={busy}>Dọn artifact trung gian</button><textarea aria-label="Project notes" placeholder="Subtitle/editor state sẽ được nối ở phase editor." /></> : <p className="muted">Project state sẽ xuất hiện ở đây.</p>}</div></section>
        <section className="bottom-grid"><div className="timeline panel"><div className="panel-heading"><span>Timeline</span><span className="muted">Frame-accurate editor · {segments.length} segments</span></div><div className="timeline-ruler"><span>00:00</span><span>00:30</span><span>01:00</span><span>01:30</span></div><div className="track"><span className="track-label">SUBTITLE</span><div className="clip">{segments.length ? `${segments.length} translated cues` : "Translated subtitle"}</div></div><div className="track"><span className="track-label">AUDIO</span><div className="clip muted-clip">Original / TTS</div></div>{segments.length > 0 && <div className="segment-list">{segments.slice(0, 8).map((segment, index) => <label key={`${index}-${segment.start}`}><span>{index + 1} · {segment.start.toFixed(2)}–{segment.end.toFixed(2)}</span><input value={segment.text} onChange={(event) => setSegments((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, text: event.target.value } : item))} /></label>)}</div>}</div><div className="logs panel"><div className="panel-heading"><span>Activity</span><span className="muted">{job?.progress ?? 0}%</span></div><div className="log-list">{logs.length ? logs.map((line, index) => <div key={`${line}-${index}`}>{line}</div>) : <span className="muted">{message || "Sẵn sàng."}</span>}</div></div></section>
      </>}
    </main>
  </div>;
}

export default App;
