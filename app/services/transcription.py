"""Transcrição com chunking apenas quando necessário, com fallback robusto."""
from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple

import google.generativeai as genai
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)

CHUNK_DURATION_SEC = 12 * 60      # 12 min por chunk
SIZE_THRESHOLD_BYTES = 25 * 1024 * 1024   # 25 MB → divide
DURATION_THRESHOLD_SEC = 13 * 60          # > 13 min → divide
MAX_PARALLEL_WORKERS = 3
MAX_RETRIES = 2
MODEL_NAME = "gemini-flash-latest"


def _get_ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _probe_duration(ffmpeg_exe: str, audio_path: str) -> Optional[float]:
    try:
        result = subprocess.run(
            [ffmpeg_exe, "-i", audio_path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        for line in result.stderr.splitlines():
            line = line.strip()
            if line.startswith("Duration:"):
                parte = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = parte.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception as e:
        print(f"[Transcrição] Probe falhou: {e}")
    return None


def _split_audio(ffmpeg_exe, audio_path, out_dir, duration_sec) -> List[Tuple[int, str]]:
    chunks = []
    idx = 0
    start = 0.0
    while start < duration_sec:
        out_path = str(out_dir / f"chunk_{idx:03d}.m4a")
        cmd = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", audio_path,
            "-t", f"{CHUNK_DURATION_SEC + 2}",
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "aac", "-b:a", "64k", out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            break
        chunks.append((idx, out_path))
        idx += 1
        start += CHUNK_DURATION_SEC
    return chunks


_PROMPT = "Transcreva este áudio em português. Retorne APENAS o texto, sem comentários, sem timestamps."


def _transcribe_single(audio_path: str, label: str = "") -> str:
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        audio_file = None
        try:
            audio_file = genai.upload_file(path=audio_path)
            model = genai.GenerativeModel(MODEL_NAME)
            response = model.generate_content(
                [_PROMPT, audio_file],
                request_options={"timeout": 240},
            )
            text = (response.text or "").strip()
            print(f"[Transcrição] {label} ok ({len(text)} chars)")
            return text
        except Exception as e:
            last_err = e
            print(f"[Transcrição] {label} tentativa {attempt} falhou: {e}")
            time.sleep(2 ** attempt)
        finally:
            if audio_file is not None:
                try: audio_file.delete()
                except Exception: pass
    raise RuntimeError(f"Chunk {label} falhou: {last_err}")


def transcrever(audio_path: str) -> str:
    size_bytes = os.path.getsize(audio_path)
    ffmpeg_exe = _get_ffmpeg_exe()

    if ffmpeg_exe is None:
        print("[Transcrição] ffmpeg indisponível — upload único.")
        return _transcribe_single(audio_path, label="full")

    duration = _probe_duration(ffmpeg_exe, audio_path)
    precisa_dividir = (
        size_bytes > SIZE_THRESHOLD_BYTES
        or (duration and duration > DURATION_THRESHOLD_SEC)
    )

    if not precisa_dividir:
        print(f"[Transcrição] Cabe ({size_bytes/1024/1024:.1f}MB) — upload único.")
        return _transcribe_single(audio_path, label="full")

    if not duration:
        duration = size_bytes * 8 / 64_000
    print(f"[Transcrição] Dividindo ({duration/60:.1f}min)...")

    with tempfile.TemporaryDirectory(prefix="studyai_") as tmpdir:
        try:
            chunks = _split_audio(ffmpeg_exe, audio_path, Path(tmpdir), duration)
        except Exception as e:
            print(f"[Transcrição] Split falhou ({e}) — fallback único.")
            return _transcribe_single(audio_path, label="fallback")

        if not chunks:
            return _transcribe_single(audio_path, label="fallback")

        print(f"[Transcrição] {len(chunks)} chunks. Paralelizando...")
        resultados = {}
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS) as ex:
            futuros = {ex.submit(_transcribe_single, p, f"c{i:02d}"): i for i, p in chunks}
            for fut in as_completed(futuros):
                try:
                    resultados[futuros[fut]] = fut.result()
                except Exception as e:
                    print(f"[Transcrição] chunk falhou: {e}")
                    resultados[futuros[fut]] = ""

        return "\n\n".join(resultados[i] for i in sorted(resultados) if resultados[i]).strip()
