"""Transcricao otimizada: comprime preservando qualidade da fala, 1 chamada ao Gemini."""
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

MODEL_NAME = "gemini-flash-latest"
TIMEOUT_SEC = 600  # 10 min - generoso para audios longos


# Prompt otimizado para fidelidade maxima
_PROMPT_TRANSCRICAO = """Voce e um transcritor profissional. Transcreva este audio em portugues brasileiro com 100% de fidelidade.

REGRAS OBRIGATORIAS:
1. Transcreva TUDO o que for dito, palavra por palavra, sem omitir nada
2. Mantenha a ordem exata da fala
3. Inclua termos tecnicos, nomes proprios, numeros e siglas exatamente como pronunciados
4. Para palavras inaudiveis ou incertas, use [inaudivel]
5. Separe paragrafos a cada mudanca de assunto ou pausa longa
6. NAO adicione comentarios, NAO resuma, NAO explique
7. NAO inclua timestamps
8. NAO use marcadores tipo "Falante 1:" ou "Professor:"

Retorne APENAS o texto transcrito, limpo e organizado em paragrafos."""


def _get_ffmpeg_exe() -> Optional[str]:
    """Retorna caminho do ffmpeg ou None."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        print(f"[Transcricao] imageio-ffmpeg falhou: {e}")
        return shutil.which("ffmpeg")


def _comprimir_qualidade(ffmpeg_exe: str, input_path: str, output_dir: Path) -> Optional[str]:
    """Compressao otimizada para fala:
    - Mono (suficiente para voz, reduz pela metade)
    - 16kHz (preserva toda a banda de fala humana 80Hz-8kHz)
    - Opus 32kbps (melhor codec para fala, qualidade similar a 64kbps MP3)
    - Filtros: highpass 80Hz (remove ruido grave) + loudnorm (normaliza volume)

    Resultado: arquivo ~8-10x menor com qualidade de transcricao IGUAL ao original.
    """
    output_path = str(output_dir / "compressed.opus")

    # Compressao otimizada para fala com filtros de melhoria
    cmd = [
        ffmpeg_exe, "-y", "-loglevel", "error",
        "-i", input_path,
        "-vn",                           # remove video se houver
        "-ac", "1",                      # mono
        "-ar", "16000",                  # 16kHz - padrao para reconhecimento de fala
        "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11",  # remove ruido + normaliza
        "-c:a", "libopus",               # melhor codec para voz
        "-b:a", "32k",                   # 32kbps - qualidade boa para transcricao
        "-application", "voip",          # otimizado para fala
        output_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            orig_mb = os.path.getsize(input_path) / 1024 / 1024
            new_mb = os.path.getsize(output_path) / 1024 / 1024
            reducao = 100 * (1 - new_mb / orig_mb)
            print(f"[Transcricao] Comprimido (Opus 32k): {orig_mb:.1f}MB -> {new_mb:.1f}MB ({reducao:.0f}% menor)")
            return output_path

        # Fallback 1: Opus simples sem filtros
        print(f"[Transcricao] Opus com filtros falhou, tentando sem filtros...")
        cmd_simples = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-i", input_path,
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "libopus", "-b:a", "32k",
            "-application", "voip",
            output_path,
        ]
        proc = subprocess.run(cmd_simples, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            orig_mb = os.path.getsize(input_path) / 1024 / 1024
            new_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"[Transcricao] Comprimido (Opus simples): {orig_mb:.1f}MB -> {new_mb:.1f}MB")
            return output_path

        # Fallback 2: AAC (mais compativel)
        print(f"[Transcricao] Opus falhou, tentando AAC...")
        output_path = str(output_dir / "compressed.m4a")
        cmd_aac = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-i", input_path,
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "aac", "-b:a", "48k",
            output_path,
        ]
        proc = subprocess.run(cmd_aac, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            orig_mb = os.path.getsize(input_path) / 1024 / 1024
            new_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"[Transcricao] Comprimido (AAC): {orig_mb:.1f}MB -> {new_mb:.1f}MB")
            return output_path

        print(f"[Transcricao] Compressao falhou: {proc.stderr[-300:]}")
        return None
    except Exception as e:
        print(f"[Transcricao] Erro na compressao: {e}")
        return None


def _aguardar_upload(audio_file, timeout=180):
    """Aguarda Gemini processar o upload (max 3 min)."""
    wait = 0
    while audio_file.state.name == "PROCESSING":
        time.sleep(2)
        wait += 2
        if wait > timeout:
            raise RuntimeError(f"Upload demorou mais de {timeout}s para processar")
        audio_file = genai.get_file(audio_file.name)
        if wait % 10 == 0:
            print(f"[Transcricao] ...processando upload ({wait}s)")
    if audio_file.state.name == "FAILED":
        raise RuntimeError("Upload falhou no servidor do Gemini")
    return audio_file


def transcrever(audio_path: str) -> str:
    """Funcao principal: comprime + transcreve fielmente em uma chamada."""
    t_total = time.time()
    size_mb = os.path.getsize(audio_path) / 1024 / 1024
    print(f"[Transcricao] === INICIO === ({size_mb:.1f}MB)")

    ffmpeg_exe = _get_ffmpeg_exe()

    # Define qual arquivo enviar
    audio_para_enviar = audio_path
    tmpdir_obj = None

    if ffmpeg_exe is None:
        print("[Transcricao] AVISO: ffmpeg indisponivel - enviando arquivo original")
    else:
        # Comprime preservando qualidade
        tmpdir_obj = tempfile.TemporaryDirectory(prefix="studyai_")
        comprimido = _comprimir_qualidade(ffmpeg_exe, audio_path, Path(tmpdir_obj.name))
        if comprimido and os.path.exists(comprimido):
            audio_para_enviar = comprimido
        else:
            print("[Transcricao] AVISO: compressao falhou - enviando original")

    audio_file = None
    try:
        # Upload
        print(f"[Transcricao] Enviando para Gemini ({os.path.getsize(audio_para_enviar)/1024/1024:.1f}MB)...")
        t_upload = time.time()
        audio_file = genai.upload_file(path=audio_para_enviar)
        audio_file = _aguardar_upload(audio_file)
        print(f"[Transcricao] Upload OK em {time.time()-t_upload:.1f}s")

        # Transcricao
        print(f"[Transcricao] Pedindo transcricao fiel ao Gemini...")
        t_trans = time.time()
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(
            [_PROMPT_TRANSCRICAO, audio_file],
            request_options={"timeout": TIMEOUT_SEC},
            generation_config={
                "temperature": 0.1,        # baixa = maior fidelidade ao audio
                "top_p": 0.95,
                "max_output_tokens": 32000, # limite alto para audios longos
            },
        )
        text = (response.text or "").strip()

        if not text or len(text) < 30:
            raise RuntimeError(f"Transcricao retornou vazia ou muito curta ({len(text)} chars)")

        print(f"[Transcricao] === SUCESSO === {len(text)} chars em {time.time()-t_trans:.1f}s")
        print(f"[Transcricao] Tempo total: {time.time()-t_total:.1f}s")
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
