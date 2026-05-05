"""Transcrição agressiva: comprime forte, divide pequeno, paraleliza muito."""
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

# Configuração agressiva
CHUNK_DURATION_SEC = 8 * 60       # 8 min por chunk (era 12)
SIZE_THRESHOLD_BYTES = 8 * 1024 * 1024   # 8 MB → divide
DURATION_THRESHOLD_SEC = 9 * 60          # > 9 min → divide
MAX_PARALLEL_WORKERS = 4
MAX_RETRIES = 2
MODEL_NAME = "gemini-flash-latest"
CHUNK_TIMEOUT_SEC = 90  # timeout por chunk (era 240)


def _get_ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        print(f"[Transcrição] imageio-ffmpeg falhou: {e}")
        return shutil.which("ffmpeg")


def _comprimir_agressivo(ffmpeg_exe: str, input_path: str, output_path: str) -> bool:
    """Comprime brutalmente: mono, 12kHz, Opus 24kbps. Áudio de fala fica perfeito.
    Reduz arquivo de ~50MB pra ~5MB.
    """
    cmd = [
        ffmpeg_exe, "-y", "-loglevel", "error",
        "-i", input_path,
        "-vn",                  # remove vídeo
        "-ac", "1",             # mono
        "-ar", "12000",         # 12kHz (fala humana cobre até ~4kHz)
        "-c:a", "libopus",      # opus = melhor compressão pra fala
        "-b:a", "24k",          # 24kbps - super baixo mas legível
        "-application", "voip", # otimizado pra fala
        output_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            orig_mb = os.path.getsize(input_path) / 1024 / 1024
            new_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"[Transcrição] Comprimido: {orig_mb:.1f}MB → {new_mb:.1f}MB ({100*(1-new_mb/orig_mb):.0f}% menor)")
            return True
        # Fallback: tenta com AAC se opus falhar
        cmd_aac = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-i", input_path,
            "-vn", "-ac", "1", "-ar", "12000",
            "-c:a", "aac", "-b:a", "32k",
            output_path.replace(".opus", ".m4a"),
        ]
        proc = subprocess.run(cmd_aac, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0:
            print(f"[Transcrição] Comprimido com AAC fallback")
            return True
        print(f"[Transcrição] Compressão falhou: {proc.stderr[-300:]}")
        return False
    except Exception as e:
        print(f"[Transcrição] Erro na compressão: {e}")
        return False


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
        out_path = str(out_dir / f"chunk_{idx:03d}.opus")
        cmd = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", audio_path,
            "-t", f"{CHUNK_DURATION_SEC + 2}",
            "-vn", "-ac", "1", "-ar", "12000",
            "-c:a", "libopus", "-b:a", "24k",
            "-application", "voip",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            # Fallback AAC
            out_path = out_path.replace(".opus", ".m4a")
            cmd[-2:-1] = ["aac"]; cmd[-3:-2] = ["32k"]
            cmd = [c for c in cmd if c != "voip" and c != "-application"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if proc.returncode != 0 or not os.path.exists(out_path):
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
