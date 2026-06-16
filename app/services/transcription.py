"""Transcricao: compacta se arquivo > 24MB (limite Whisper = 25MB). Caso contrario sobe original."""
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from openai import OpenAI
from app.config import settings

_client = OpenAI(api_key=settings.openai_api_key)

TIMEOUT_WHISPER = 300             # 5 min para Whisper transcrever
TIMEOUT_COMPRESSAO = 90           # 90s no MAX para compactar
SKIP_COMPRESSION_BELOW_MB = 24.0  # Whisper aceita ate 25MB
SPEED_FACTOR = 1.4                # equilibrio: ganho real sem perder termos medicos


# ============================================================
# COMPACTACAO COM FFMPEG (so para arquivos > 24MB)
# ============================================================

def _achar_ffmpeg() -> Optional[str]:
    """Tenta achar ffmpeg via imageio-ffmpeg ou no PATH."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        print(f"[Compactacao] imageio_ffmpeg indisponivel: {e}")
    return shutil.which("ffmpeg")


def _compactar_audio_simples(ffmpeg_exe: str, input_path: str,
                              output_dir: Path) -> Optional[str]:
    """Compactacao simples e rapida: speed 1.4x + Opus 24kbps mono."""
    output_path = str(output_dir / "compressed.opus")

    cmd = [
        ffmpeg_exe, "-y", "-loglevel", "error",
        "-threads", "0",
        "-i", input_path,
        "-vn",                     # sem video
        "-ac", "1",                # mono
        "-ar", "16000",            # 16 kHz
        "-af", f"atempo={SPEED_FACTOR}",
        "-c:a", "libopus",
        "-b:a", "24k",             # 24 kbps voz
        "-application", "voip",
        "-vbr", "on",
        output_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=TIMEOUT_COMPRESSAO)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return output_path
        print(f"[Compactacao] Falhou: rc={proc.returncode}, "
              f"stderr={(proc.stderr or '')[-200:]}")
    except subprocess.TimeoutExpired:
        print(f"[Compactacao] Timeout apos {TIMEOUT_COMPRESSAO}s")
    except Exception as e:
        print(f"[Compactacao] Erro: {e}")

    return None


# ============================================================
# COMPACTACAO PARALELA
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
        path = _compactar_audio_simples(ffmpeg_exe, input_path, output_dir)
        result.path = path
    except Exception as e:
        print(f"[Compactacao Thread] Erro: {e}")
    finally:
        result.elapsed = time.time() - t_start
        result.done = True


# ============================================================
# FUNCAO PRINCIPAL
# ============================================================

def transcrever(audio_path: str) -> str:
    """Transcreve audio via Whisper (OpenAI). Compacta se > 24MB."""
    t_total = time.time()
    size_mb_original = os.path.getsize(audio_path) / 1024 / 1024
    print(f"[Transcricao] === INICIO === arquivo {size_mb_original:.1f}MB")

    audio_para_enviar = audio_path
    tmpdir_obj = None
    compact_thread = None
    compact_result = None

    try:
        # === ETAPA 1: Decide se vale tentar compactar ===
        ffmpeg_exe = None
        if size_mb_original >= SKIP_COMPRESSION_BELOW_MB:
            print(f"[Transcricao] Arquivo grande ({size_mb_original:.1f}MB >= "
                  f"{SKIP_COMPRESSION_BELOW_MB}MB), tentando compactar")
            ffmpeg_exe = _achar_ffmpeg()
        else:
            print(f"[Transcricao] Arquivo OK ({size_mb_original:.1f}MB < "
                  f"{SKIP_COMPRESSION_BELOW_MB}MB), pulando compactacao")

        # === ETAPA 2: Inicia compactacao em paralelo (se aplicavel) ===
        if ffmpeg_exe:
            print(f"[Transcricao] Iniciando compactacao em paralelo "
                  f"(speed {SPEED_FACTOR}x, timeout {TIMEOUT_COMPRESSAO}s)")
            tmpdir_obj = tempfile.TemporaryDirectory(prefix="studyai_audio_")
            compact_result = _CompactResult()
            compact_thread = threading.Thread(
                target=_compactar_em_thread,
                args=(ffmpeg_exe, audio_path, Path(tmpdir_obj.name), compact_result),
                daemon=True,
            )
            compact_thread.start()

        # === ETAPA 3: Espera compactacao OU usa original apos timeout ===
        if compact_thread:
            print(f"[Transcricao] Aguardando compactacao terminar...")
            compact_thread.join(timeout=TIMEOUT_COMPRESSAO + 5)

            if compact_result.done and compact_result.path:
                novo_mb = os.path.getsize(compact_result.path) / 1024 / 1024
                reducao_pct = (1 - novo_mb / size_mb_original) * 100
                print(f"[Transcricao] Compactacao OK em {compact_result.elapsed:.1f}s: "
                      f"{size_mb_original:.1f}MB -> {novo_mb:.1f}MB "
                      f"(-{reducao_pct:.0f}%)")
                audio_para_enviar = compact_result.path
            else:
                if not compact_result.done:
                    print(f"[Transcricao] Compactacao ainda rodando apos timeout, "
                          f"abandonando e usando original")
                else:
                    print(f"[Transcricao] Compactacao falhou em {compact_result.elapsed:.1f}s, "
                          f"usando original")

        # === ETAPA 4: Transcricao via Whisper ===
        upload_size_mb = os.path.getsize(audio_para_enviar) / 1024 / 1024
        print(f"[Transcricao] Enviando {upload_size_mb:.1f}MB ao Whisper...")
        t_tr = time.time()

        with open(audio_para_enviar, "rb") as f:
            response = _client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="pt",
                prompt=(
                    "Transcreva este audio completamente em portugues brasileiro. "
                    "Use [inaudivel] para trechos incompreensiveis. "
                    "Mantenha termos tecnicos e medicos exatamente como pronunciados."
                ),
            )

        text = (response.text or "").strip()
        print(f"[Transcricao] Resposta em {time.time()-t_tr:.1f}s, "
              f"{len(text)} caracteres")

        if not text or len(text) < 30:
            raise RuntimeError(f"Resposta vazia ou muito curta ({len(text)} chars)")

        print(f"[Transcricao] === OK === total {time.time()-t_total:.1f}s, "
              f"{len(text)} caracteres")
        return text

    except Exception as e:
        elapsed = time.time() - t_total
        print(f"[Transcricao] === ERRO apos {elapsed:.1f}s === "
              f"{type(e).__name__}: {e}")
        raise

    finally:
        if tmpdir_obj is not None:
            try:
                tmpdir_obj.cleanup()
            except Exception:
                pass
