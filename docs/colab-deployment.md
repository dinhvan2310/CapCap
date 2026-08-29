# CapCap Colab deployment

Bản Colab mới dùng `notebooks/capcap_colab_tailscale.ipynb`.

## Chuẩn bị

1. Trên máy phát triển, chạy `npm install` và `npm run build` trong `frontend/`.
2. Upload/clone cả repository vào Colab để có `frontend/dist`.
3. Trong Colab Secrets tạo `TAILSCALE_AUTH_KEY`. Nếu Ollama Cloud yêu cầu, thêm `OLLAMA_API_KEY`.
4. Máy Windows cài Tailscale và đăng nhập cùng tailnet.

## Luồng chạy

Notebook cài dependency, khởi động Ollama và pull model vào `/content`, chạy Tailscale ở userspace mode, chạy FastAPI trên `127.0.0.1:8765`, rồi dùng `tailscale serve` để cấp HTTPS nội bộ tailnet.

Mỗi runtime sinh một `CAPCAP_APP_TOKEN`. Mở URL trong output `tailscale serve status`, dán token vào UI. Token không được ghi vào notebook, Drive, repo hoặc URL.

## Storage

- `/content/capcap`: video, audio, TTS, preview, export và artifact trung gian.
- `/content/drive/MyDrive/CapCap/projects`: project metadata và trạng thái resume.
- `CAPCAP_MAX_UPLOAD_BYTES`: giới hạn upload, mặc định 100 GiB.

Nếu runtime reset, upload lại đúng video. Server dùng SHA-256 để khớp project. Metadata được phục hồi, nhưng artifact đã mất sẽ yêu cầu Rebuild phase.
