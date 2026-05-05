"""Transcricao agressiva: comprime forte, divide pequeno, paraleliza muito."""
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

CHUNK_DURATION_SEC = 8 * 60
SIZE_THRESHOLD_BYTES = 8 * 1024 * 1024
DURATION_THRESHOLD_SEC = 9 * 60
MAX_PARALLEL_WORKERS = 4
MAX_RETRIES = 2
MODEL_NAME = "gemini-flash-latest"
CHUNK_TIMEOUT_SEC = 90


def _get_ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        print(f"[Transcricao] imageio-ffmpeg falhou: {e}")
        return shutil.which("ffmpeg")


def _comprimir_agressivo(ffmpeg_exe: str, input_path: str, output_path: str) -> Optional[str]:
    """Comprime: mono, 12kHz, Opus 24kbps. Retorna caminho do arquivo comprimido ou None."""
    cmd = [
        ffmpeg_exe, "-y", "-loglevel", "error",
        "-i", input_path,
        "-vn",
        "-ac", "1",
        "-ar", "12000",
        "-c:a", "libopus",
        "-b:a", "24k",
        "-application", "voip",
        output_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            orig_mb = os.path.getsize(input_path) / 1024 / 1024
            new_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"[Transcricao] Comprimido: {orig_mb:.1f}MB -> {new_mb:.1f}MB")
            return output_path

        aac_path = output_path.rsplit(".", 1)[0] + ".m4a"
        cmd_aac = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-i", input_path,
            "-vn", "-ac", "1", "-ar", "12000",
            "-c:a", "aac", "-b:a", "32k",
            aac_path,
        ]
        proc = subprocess.run(cmd_aac, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0 and os.path.exists(aac_path) and os.path.getsize(aac_path) > 0:
            print(f"[Transcricao] Comprimido com AAC fallback")
            return aac_path

        print(f"[Transcricao] Compressao falhou: {proc.stderr[-300:]}")
        return None
    except Exception as e:
        print(f"[Transcricao] Erro na compressao: {e}")
        return None


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
        print(f"[Transcricao] Probe falhou: {e}")
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
            out_path = str(out_dir / f"chunk_{idx:03d}.m4a")
            cmd_aac = [
                ffmpeg_exe, "-y", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-i", audio_path,
                "-t", f"{CHUNK_DURATION_SEC + 2}",
                "-vn", "-ac", "1", "-ar", "12000",
                "-c:a", "aac", "-b:a", "32k",
                out_path,
            ]
            proc = subprocess.run(cmd_aac, capture_output=True, text=True, timeout=180)
            if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                break

        chunks.append((idx, out_path))
        idx += 1
        start += CHUNK_DURATION_SEC
    return chunks


_PROMPT = "Transcreva este audio em portugues. Retorne APENAS o texto, sem comentarios, sem timestamps."


def _transcribe_single(audio_path: str, label: str = "") -> str:
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        audio_file = None
        try:
            audio_file = genai.upload_file(path=audio_path)
            model = genai.GenerativeModel(MODEL_NAME)
            response = model.generate_content(
                [_PROMPT, audio_file],
                request_options={"timeout": CHUNK_TIMEOUT_SEC},
            )
            text = (response.text or "").strip()
            print(f"[Transcricao] {label} ok ({len(text)} chars)")
            return text
        except Exception as e:
            last_err = e
            print(f"[Transcricao] {label} tentativa {attempt}: {e}")
            time.sleep(2 ** attempt)
        finally:
            if audio_file is not None:
                try:
                    audio_file.delete()
                except Exception:
                    pass
    print(f"[Transcricao] {label} falhou definitivamente: {last_err}")
    return ""


def transcrever(audio_path: str) -> str:
    """Funcao principal chamada pelo pipeline."""
    ffmpeg_exe = _get_ffmpeg_exe()

    if ffmpeg_exe is None:
        print("[Transcricao] ffmpeg indisponivel - upload unico.")
        return _transcribe_single(audio_path, label="full")

    with tempfile.TemporaryDirectory(prefix="studyai_comp_") as comp_dir:
        compressed_target = str(Path(comp_dir) / "compressed.opus")
        compressed = _comprimir_agressivo(ffmpeg_exe, audio_path, compressed_target)

        if compressed and os.path.exists(compressed):
            audio_to_use = compressed
        else:
            print("[Transcricao] Compressao falhou, usando original")
            audio_to_use = audio_path

        size_bytes = os.path.getsize(audio_to_use)
        duration = _probe_duration(ffmpeg_exe, audio_to_use)

        precisa_dividir = (
            size_bytes > SIZE_THRESHOLD_BYTES
            or (duration and duration > DURATION_THRESHOLD_SEC)
        )

        if not precisa_dividir:
            dur_min = duration / 60 if duration else 0
            print(f"[Transcricao] Cabe ({size_bytes/1024/1024:.1f}MB, {dur_min:.1f}min) - upload unico.")
            return _transcribe_single(audio_to_use, label="full")

        if not duration:
            duration = size_bytes * 8 / 24_000

        print(f"[Transcricao] Dividindo {duration/60:.1f}min em chunks de {CHUNK_DURATION_SEC/60:.0f}min...")

        with tempfile.TemporaryDirectory(prefix="studyai_chunks_") as tmpdir:
            try:
                chunks = _split_audio(ffmpeg_exe, audio_to_use, Path(tmpdir), duration)
            except Exception as e:
                print(f"[Transcricao] Split falhou ({e}) - fallback unico.")
                return _transcribe_single(audio_to_use, label="fallback")

            if not chunks:
                print("[Transcricao] Sem chunks - fallback unico.")
                return _transcribe_single(audio_to_use, label="fallback")

            print(f"[Transcricao] {len(chunks)} chunks. Paralelizando ({MAX_PARALLEL_WORKERS} workers)...")
            resultados = {}
            with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS) as ex:
                futuros = {ex.submit(_transcribe_single, p, f"c{i:02d}"): i for i, p in chunks}
                for fut in as_completed(futuros):
                    try:
                        resultados[futuros[fut]] = fut.result(timeout=CHUNK_TIMEOUT_SEC + 30)
                    except Exception as e:
                        print(f"[Transcricao] chunk {futuros[fut]} falhou: {e}")
                        resultados[futuros[fut]] = ""

            texto = "\n\n".join(resultados[i] for i in sorted(resultados) if resultados[i]).strip()
            if not texto:
                raise RuntimeError("Todos os chunks falharam. Tente novamente em alguns minutos.")
            return texto
