"""Transcricao em 3 camadas: faster-whisper local (<=90min) > OpenAI API (>90min) > Gemini (fallback)."""
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

MODEL_GEMINI = "gemini-2.5-flash"
MODEL_GEMINI_FALLBACK = "gemini-2.5-pro"
TIMEOUT_GEMINI = 300
TIMEOUT_UPLOAD = 180
TIMEOUT_COMPRESSAO = 90
SKIP_COMPRESSION_BELOW_MB = 2.0
SPEED_FACTOR = 1.7

OPENAI_MAX_MB = 24.0               # limite da API OpenAI Whisper (25MB com margem)


# ============================================================
# UTILS: FFMPEG
# ============================================================

def _achar_ffmpeg() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


# ============================================================
# CAMADA 1: OPENAI WHISPER API
# ============================================================

def _comprimir_para_openai(audio_path: str, tmpdir: str) -> str:
    """Comprime audio para caber no limite de 25MB da OpenAI API."""
    ffmpeg_exe = _achar_ffmpeg()
    if not ffmpeg_exe:
        return audio_path

    output_path = str(Path(tmpdir) / "openai_compressed.mp3")
    cmd = [
        ffmpeg_exe, "-y", "-loglevel", "error",
        "-i", audio_path,
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", "32k",
        output_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=180, check=True)
        if os.path.exists(output_path):
            return output_path
    except Exception as e:
        print(f"[OpenAI] Compressao falhou: {e}, usando original")
    return audio_path


def _transcrever_openai(audio_path: str) -> str:
    """Transcreve via OpenAI Whisper API."""
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    size_mb = os.path.getsize(audio_path) / 1024 / 1024
    print(f"[OpenAI] Arquivo {size_mb:.1f}MB")

    arquivo_para_enviar = audio_path
    tmpdir_obj = None

    try:
        if size_mb > OPENAI_MAX_MB:
            print(f"[OpenAI] Arquivo > {OPENAI_MAX_MB}MB, comprimindo...")
            tmpdir_obj = tempfile.mkdtemp(prefix="studyai_openai_")
            arquivo_para_enviar = _comprimir_para_openai(audio_path, tmpdir_obj)
            novo_mb = os.path.getsize(arquivo_para_enviar) / 1024 / 1024
            print(f"[OpenAI] Comprimido: {size_mb:.1f}MB -> {novo_mb:.1f}MB")

        print(f"[OpenAI] Enviando para Whisper API...")
        t0 = time.time()
        with open(arquivo_para_enviar, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="pt",
                response_format="text",
            )
        texto = (result or "").strip()
        print(f"[OpenAI] OK em {time.time()-t0:.1f}s, {len(texto)} chars")
        return texto
    finally:
        if tmpdir_obj:
            shutil.rmtree(tmpdir_obj, ignore_errors=True)


# ============================================================
# CAMADA 2: GEMINI (FALLBACK)
# ============================================================

class _CompactResult:
    def __init__(self):
        self.done = False
        self.path = None
        self.elapsed = 0.0


def _compactar_em_thread(ffmpeg_exe: str, input_path: str, output_dir: Path,
                          result: _CompactResult):
    t_start = time.time()
    try:
        output_path = str(output_dir / "compressed.opus")
        cmd = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-threads", "0", "-i", input_path,
            "-vn", "-ac", "1", "-ar", "16000",
            "-af", f"atempo={SPEED_FACTOR}",
            "-c:a", "libopus", "-b:a", "24k",
            "-application", "voip", "-vbr", "on",
            output_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_COMPRESSAO)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            result.path = output_path
        else:
            print(f"[Compactacao] Falhou: rc={proc.returncode}")
    except subprocess.TimeoutExpired:
        print(f"[Compactacao] Timeout apos {TIMEOUT_COMPRESSAO}s")
    except Exception as e:
        print(f"[Compactacao] Erro: {e}")
    finally:
        result.elapsed = time.time() - t_start
        result.done = True


def _aguardar_upload_pronto(audio_file, timeout=TIMEOUT_UPLOAD):
    start = time.time()
    last_log = 0
    while audio_file.state.name == "PROCESSING":
        elapsed = time.time() - start
        if elapsed > timeout:
            raise RuntimeError(f"Upload travou em PROCESSING apos {timeout}s.")
        if int(elapsed) - last_log >= 5:
            print(f"[Gemini] Aguardando upload processar... ({int(elapsed)}s)")
            last_log = int(elapsed)
        time.sleep(2)
        audio_file = genai.get_file(audio_file.name)
    if audio_file.state.name != "ACTIVE":
        raise RuntimeError(f"Estado inesperado do upload: {audio_file.state.name}")
    return audio_file


def _transcrever_gemini(audio_path: str) -> str:
    """Transcreve via Gemini (fallback final). Comprime e faz upload em paralelo."""
    size_mb = os.path.getsize(audio_path) / 1024 / 1024
    print(f"[Gemini] === INICIO === arquivo {size_mb:.1f}MB")

    tmpdir_obj = None
    compact_thread = None
    compact_result = None
    audio_file = None
    upload_original_file = None

    try:
        ffmpeg_exe = _achar_ffmpeg() if size_mb >= SKIP_COMPRESSION_BELOW_MB else None

        if ffmpeg_exe:
            tmpdir_obj = tempfile.TemporaryDirectory(prefix="studyai_audio_")
            compact_result = _CompactResult()
            compact_thread = threading.Thread(
                target=_compactar_em_thread,
                args=(ffmpeg_exe, audio_path, Path(tmpdir_obj.name), compact_result),
                daemon=True,
            )
            compact_thread.start()

        if compact_thread:
            print(f"[Gemini] Upload do original em paralelo com compactacao...")
            upload_original_file = genai.upload_file(path=audio_path)
            compact_thread.join(timeout=TIMEOUT_COMPRESSAO + 5)

            if compact_result.done and compact_result.path:
                novo_mb = os.path.getsize(compact_result.path) / 1024 / 1024
                print(f"[Gemini] Compactacao OK: {size_mb:.1f}MB -> {novo_mb:.1f}MB")
                try:
                    genai.delete_file(upload_original_file.name)
                    upload_original_file = None
                except Exception:
                    pass
                audio_file = genai.upload_file(path=compact_result.path)
            else:
                print(f"[Gemini] Compactacao falhou/timeout, usando original ja em upload")
                audio_file = upload_original_file
                upload_original_file = None
        else:
            print(f"[Gemini] Upload direto de {size_mb:.1f}MB...")
            audio_file = genai.upload_file(path=audio_path)

        audio_file = _aguardar_upload_pronto(audio_file)
        print(f"[Gemini] Upload pronto (ACTIVE)")

        PROMPT = (
            "Voce e um transcritor especializado em aulas academicas em portugues brasileiro. "
            "Transcreva COMPLETAMENTE este audio, palavra por palavra, sem omitir nada. "
            "REGRAS: 1. Retorne APENAS o texto transcrito. "
            "2. Use [inaudivel] para trechos incompreensiveis. "
            "3. Preserve termos tecnicos exatamente como pronunciados. "
            "4. Separe em paragrafos curtos por mudanca de topico. "
            "5. NAO resuma, transcreva absolutamente tudo."
        )
        GEN_CONFIG = {"temperature": 0.0, "max_output_tokens": 32000}

        texto = ""
        for model_name in (MODEL_GEMINI, MODEL_GEMINI_FALLBACK):
            print(f"[Gemini] Tentando {model_name}...")
            t_tr = time.time()
            try:
                model = genai.GenerativeModel(model_name)
                resp = model.generate_content(
                    [PROMPT, audio_file],
                    request_options={"timeout": TIMEOUT_GEMINI},
                    generation_config=GEN_CONFIG,
                )
                texto = (resp.text or "").strip()
                print(f"[Gemini] {model_name} OK em {time.time()-t_tr:.1f}s, {len(texto)} chars")
                if texto and len(texto) >= 30:
                    break
            except Exception as e:
                print(f"[Gemini] {model_name} falhou: {type(e).__name__}: {e}")

        if not texto or len(texto) < 30:
            raise RuntimeError(f"Transcricao Gemini vazia ({len(texto)} chars)")
        return texto

    finally:
        for f in (audio_file, upload_original_file):
            if f is not None:
                try:
                    genai.delete_file(f.name)
                except Exception:
                    pass
        if tmpdir_obj is not None:
            try:
                tmpdir_obj.cleanup()
            except Exception:
                pass


# ============================================================
# FUNCAO PRINCIPAL
# ============================================================

def transcrever(audio_path: str) -> str:
    """Transcreve audio: OpenAI API (pago, rápido) > Gemini (fallback)."""
    t_total = time.time()
    size_mb = os.path.getsize(audio_path) / 1024 / 1024
    print(f"[Transcricao] === INICIO === {size_mb:.1f}MB")

    # Camada 1: OpenAI Whisper API (pago, rapido, qualidade alta)
    if settings.openai_api_key:
        try:
            texto = _transcrever_openai(audio_path)
            if texto and len(texto) >= 30:
                print(f"[Transcricao] === OK (OpenAI) === {time.time()-t_total:.1f}s total")
                return texto
            print(f"[Transcricao] OpenAI retornou texto curto, tentando Gemini...")
        except Exception as e:
            print(f"[Transcricao] OpenAI falhou: {type(e).__name__}: {e}")
    else:
        print(f"[Transcricao] OPENAI_API_KEY nao configurada, usando Gemini")

    # Camada 2: Gemini (fallback sempre disponivel)
    print(f"[Transcricao] Usando Gemini como fallback...")
    try:
        texto = _transcrever_gemini(audio_path)
        print(f"[Transcricao] === OK (Gemini) === {time.time()-t_total:.1f}s total")
        return texto
    except Exception as e:
        elapsed = time.time() - t_total
        print(f"[Transcricao] === ERRO apos {elapsed:.1f}s === {type(e).__name__}: {e}")
        raise
