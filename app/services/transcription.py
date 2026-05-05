"""Transcricao simples: comprime forte e manda numa unica chamada ao Gemini."""
from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import google.generativeai as genai
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)

MAX_RETRIES = 3
MODEL_NAME = "gemini-flash-latest"
TIMEOUT_SEC = 600  # 10 min - generoso pra audio comprimido inteiro


def _get_ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        print(f"[Transcricao] imageio-ffmpeg falhou: {e}")
        return shutil.which("ffmpeg")


def _comprimir(ffmpeg_exe: str, input_path: str, output_dir: Path) -> Optional[str]:
    """Comprime brutalmente: mono, 12kHz, Opus 24kbps. ~10x menor.
    Retorna caminho do arquivo comprimido. Tem fallback AAC se Opus falhar.
    """
    output_path = str(output_dir / "compressed.opus")
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            orig_mb = os.path.getsize(input_path) / 1024 / 1024
            new_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"[Transcricao] Comprimido (Opus): {orig_mb:.1f}MB -> {new_mb:.1f}MB")
            return output_path

        # Fallback AAC
        print(f"[Transcricao] Opus falhou, tentando AAC: {proc.stderr[-200:]}")
        output_path = str(output_dir / "compressed.m4a")
        cmd_aac = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-i", input_path,
            "-vn", "-ac", "1", "-ar", "12000",
            "-c:a", "aac", "-b:a", "32k",
            output_path,
        ]
        proc = subprocess.run(cmd_aac, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            orig_mb = os.path.getsize(input_path) / 1024 / 1024
            new_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"[Transcricao] Comprimido (AAC): {orig_mb:.1f}MB -> {new_mb:.1f}MB")
            return output_path

        print(f"[Transcricao] Compressao falhou totalmente: {proc.stderr[-300:]}")
        return None
    except Exception as e:
        print(f"[Transcricao] Erro na compressao: {e}")
        return None


_PROMPT = "Transcreva este audio em portugues. Retorne APENAS o texto, sem comentarios, sem timestamps."


def _transcribe(audio_path: str) -> str:
    """Faz upload do audio e pede transcricao com retry."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        audio_file = None
        try:
            print(f"[Transcricao] Tentativa {attempt}/{MAX_RETRIES}: enviando audio ({os.path.getsize(audio_path)/1024/1024:.1f}MB)...")
            audio_file = genai.upload_file(path=audio_path)

            # Espera o upload processar
            while audio_file.state.name == "PROCESSING":
                time.sleep(2)
                audio_file = genai.get_file(audio_file.name)

            if audio_file.state.name == "FAILED":
                raise RuntimeError("Upload falhou no servidor do Gemini")

            print(f"[Transcricao] Upload ok. Pedindo transcricao...")
            model = genai.GenerativeModel(MODEL_NAME)
            response = model.generate_content(
                [_PROMPT, audio_file],
                request_options={"timeout": TIMEOUT_SEC},
            )
            text = (response.text or "").strip()
            print(f"[Transcricao] Concluida ({len(text)} chars)")
            return text
        except Exception as e:
            last_err = e
            print(f"[Transcricao] Tentativa {attempt} falhou: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)
        finally:
            if audio_file is not None:
                try:
                    genai.delete_file(audio_file.name)
                except Exception:
                    pass
    raise RuntimeError(f"Transcricao falhou apos {MAX_RETRIES} tentativas: {last_err}")


def transcrever(audio_path: str) -> str:
    """Funcao principal: comprime + transcreve numa unica chamada."""
    ffmpeg_exe = _get_ffmpeg_exe()

    # Sem ffmpeg: tenta direto (ultimo recurso)
    if ffmpeg_exe is None:
        print("[Transcricao] ffmpeg indisponivel - upload direto.")
        return _transcribe(audio_path)

    # Comprime e transcreve
    with tempfile.TemporaryDirectory(prefix="studyai_") as tmpdir:
        compressed = _comprimir(ffmpeg_exe, audio_path, Path(tmpdir))

        if compressed and os.path.exists(compressed):
            return _transcribe(compressed)
        else:
            print("[Transcricao] Compressao falhou, usando original")
            return _transcribe(audio_path)
