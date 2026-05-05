"""Transcricao maxima velocidade: 1.5x speed + skip silencio + compressao paralela."""
from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import google.generativeai as genai
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)

MODEL_NAME = "gemini-flash-latest"
TIMEOUT_SEC = 480
SKIP_COMPRESSION_BELOW_MB = 8
SPEED_FACTOR = 1.5  # 1.5x velocidade - reduz duracao em 33% sem perder fidelidade


_PROMPT = """Transcreva este audio em portugues brasileiro com fidelidade total.

Regras:
- Transcreva TUDO palavra por palavra, sem omitir
- Mantenha ordem e termos exatos (incluindo siglas, nomes proprios, termos tecnicos)
- Use [inaudivel] para trechos incompreensiveis
- Separe em paragrafos por mudanca de assunto
- NAO comente, NAO resuma, NAO use timestamps ou marcadores

Retorne apenas o texto."""


def _get_ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _comprimir_turbo(ffmpeg_exe: str, input_path: str, output_dir: Path) -> Optional[str]:
    """Compressao maxima velocidade:
    - 1.5x speed (atempo) - reduz duracao em 33%
    - silenceremove - remove pausas longas (acelera mais ainda)
    - 12kHz mono Opus 24kbps voip
    """
    output_path = str(output_dir / "compressed.opus")

    # Filter chain: speed up 1.5x + remove silencios > 0.8s
    audio_filter = (
        f"atempo={SPEED_FACTOR},"
        "silenceremove=stop_periods=-1:stop_duration=0.8:stop_threshold=-40dB"
    )

    cmd = [
        ffmpeg_exe, "-y", "-loglevel", "error",
        "-threads", "0",
        "-i", input_path,
        "-vn",
        "-ac", "1",
        "-ar", "12000",
        "-af", audio_filter,
        "-c:a", "libopus",
        "-b:a", "24k",
        "-application", "voip",
        "-vbr", "on",
        "-compression_level", "5",
        output_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            orig_mb = os.path.getsize(input_path) / 1024 / 1024
            new_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"[Transcricao] Comprimido (1.5x + skip silencio): {orig_mb:.1f}MB -> {new_mb:.1f}MB")
            return output_path

        # Fallback 1: speed up sem skip silencio
        print(f"[Transcricao] Tentando sem skip silencio...")
        cmd_simples = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-threads", "0",
            "-i", input_path,
            "-vn", "-ac", "1", "-ar", "12000",
            "-af", f"atempo={SPEED_FACTOR}",
            "-c:a", "libopus", "-b:a", "24k",
            "-application", "voip",
            output_path,
        ]
        proc = subprocess.run(cmd_simples, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0 and os.path.exists(output_path):
            orig_mb = os.path.getsize(input_path) / 1024 / 1024
            new_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"[Transcricao] Comprimido (1.5x): {orig_mb:.1f}MB -> {new_mb:.1f}MB")
            return output_path

        # Fallback 2: AAC sem nada
        print(f"[Transcricao] Tentando AAC simples...")
        output_path = str(output_dir / "compressed.m4a")
        cmd_aac = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-threads", "0",
            "-i", input_path,
            "-vn", "-ac", "1", "-ar", "12000",
            "-c:a", "aac", "-b:a", "32k",
            output_path,
        ]
        proc = subprocess.run(cmd_aac, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0 and os.path.exists(output_path):
            orig_mb = os.path.getsize(input_path) / 1024 / 1024
            new_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"[Transcricao] Comprimido (AAC fallback): {orig_mb:.1f}MB -> {new_mb:.1f}MB")
            return output_path

        print(f"[Transcricao] Compressao falhou: {proc.stderr[-200:]}")
        return None
    except Exception as e:
        print(f"[Transcricao] Erro compressao: {e}")
        return None


def _aguardar_upload(audio_file, timeout=120):
    wait = 0
    while audio_file.state.name == "PROCESSING":
        time.sleep(1)
        wait += 1
        if wait > timeout:
            raise RuntimeError(f"Upload travou apos {timeout}s")
        audio_file = genai.get_file(audio_file.name)
    if audio_file.state.name == "FAILED":
        raise RuntimeError("Upload falhou no Gemini")
    return audio_file


def transcrever(audio_path: str) -> str:
    """Transcricao turbo: speed 1.5x + skip silencio + compressao."""
    t_total = time.time()
    size_mb = os.path.getsize(audio_path) / 1024 / 1024
    print(f"[Transcricao] === TURBO === ({size_mb:.1f}MB)")

    audio_para_enviar = audio_path
    tmpdir_obj = None

    if size_mb <= SKIP_COMPRESSION_BELOW_MB:
        # Mesmo arquivos pequenos se beneficiam do speed-up
        print(f"[Transcricao] Pequeno mas vou aplicar speed 1.5x mesmo assim...")

    ffmpeg_exe = _get_ffmpeg_exe()
    if ffmpeg_exe is None:
        print("[Transcricao] AVISO: ffmpeg indisponivel - usando original")
    else:
        t_comp = time.time()
        tmpdir_obj = tempfile.TemporaryDirectory(prefix="studyai_")
        comprimido = _comprimir_turbo(ffmpeg_exe, audio_path, Path(tmpdir_obj.name))
        if comprimido:
            audio_para_enviar = comprimido
            print(f"[Transcricao] Compressao em {time.time()-t_comp:.1f}s")
        else:
            print("[Transcricao] AVISO: compressao falhou - usando original")

    audio_file = None
    try:
        upload_size = os.path.getsize(audio_para_enviar) / 1024 / 1024
        print(f"[Transcricao] Upload {upload_size:.1f}MB...")
        t_up = time.time()
        audio_file = genai.upload_file(path=audio_para_enviar)
        audio_file = _aguardar_upload(audio_file)
        print(f"[Transcricao] Upload OK em {time.time()-t_up:.1f}s")

        print(f"[Transcricao] Transcrevendo...")
        t_tr = time.time()
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(
            [_PROMPT, audio_file],
            request_options={"timeout": TIMEOUT_SEC},
            generation_config={
                "temperature": 0.1,
                "top_p": 0.9,
                "max_output_tokens": 32000,
                "candidate_count": 1,
            },
        )
        text = (response.text or "").strip()

        if not text or len(text) < 30:
            raise RuntimeError(f"Resposta vazia ({len(text)} chars)")

        print(f"[Transcricao] === OK === {len(text)} chars em {time.time()-t_tr:.1f}s | TOTAL: {time.time()-t_total:.1f}s")
        return text

    except Exception as e:
        print(f"[Transcricao] ERRO: {type(e).__name__}: {e}")
        raise

    finally:
        if audio_file is not None:
            try:
                genai.delete_file(audio_file.name)
            except Exception:
                pass
        if tmpdir_obj is not None:
            try:
                tmpdir_obj.cleanup()
            except Exception:
                pass
