CapCap GPU runtime pack: CUDA 12.8

This folder contains the CUDA 12.8 runtime DLLs used by Faster-Whisper:
- cuBLAS / cuBLASLt 12.8.3
- CUDA Runtime 12.8.57
- cuFFT 11.3.3.41
- cuDNN 9.10.2.21
- NVRTC 12.8 (`nvrtc64_120_0.dll` and `nvrtc-builtins64_128.dll`) for RapidOCR CUDA inference

Keep all DLLs from compatible CUDA/cuDNN releases together. Do not mix
individual DLLs from older CUDA packs. Resource Manager can replace this
folder when a newer GPU runtime pack is available.
