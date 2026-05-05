"""Transcricao com compactacao otimizada: mono 12kHz Opus + speed 1.4x + skip silencio.
Fallback total: se compactacao falhar por QUALQUER motivo, manda audio original."""
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
TIMEOUT_GEMINI = 300              # 5 min para Gemini transcrever
TIMEOUT_UPLOAD = 180              # 3 min para upload terminar de processar
TIMEOUT_COMPRESSAO = 180          # 3 min para compactar (apos isso desiste)
SKIP_COMPRESSION_BELOW_MB = 8.0   # Arquivo pequeno: nao vale a pena compactar
SPEED_FACTOR = 1.4                # Equilibrio: ganho real sem perder termos medicos


# ============================================================
# COMPACTACAO COM FFMPEG
# ============================================================

def _achar_ffmpeg() -> Optional[str]:
    """Tenta achar ffmpeg via imageio-ffmpeg ou no PATH do sistema."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        print(f"[Compactacao] imageio_ffmpeg indisponivel: {e}")
    return shutil.which("ffmpeg")


def _compactar_audio(ffmpeg_exe: str, input_path: str, output_dir: Path) -> Optional[str]:
    """Compacta audio com 3 niveis de fallback:
    1. Filtro completo: speed 1.4x + skip silencio + Opus 24kbps mono 12kHz
    2. Sem skip silencio: speed 1.4x + Opus 24kbps mono 12kHz
    3. AAC simples: mono 16kHz 32kbps (mais compativel)
    Retorna caminho do arquivo compactado, ou None se tudo falhar."""

    # --- Tentativa 1: filtro completo ---
    output_path = str(output_dir / "compressed.opus")
    audio_filter_completo = (
        f"atempo={SPEED_FACTOR},"
        "silenceremove=stop_periods=-1:stop_duration=0.8:stop_threshold=-40dB"
    )
    cmd_completo = [
        ffmpeg_exe, "-y", "-loglevel", "error",
        "-threads", "0",
        "-i", input_path,
        "-vn",                     # sem video
        "-ac", "1",                # mono
        "-ar", "12000",            # 12 kHz (suficiente pra voz)
        "-af", audio_filter_completo,
        "-c:a", "libopus",
        "-b:a", "24k",             # 24 kbps (qualidade voz)
        "-application", "voip",    # otimizado pra fala
        "-vbr", "on",
        "-compression_level", "5",
        output_path,
    ]
    try:
        print(f"[Compactacao] Tentativa 1: filtro completo (speed {SPEED_FACTOR}x + skip silencio)")
        proc = subprocess.run(cmd_completo, capture_output=True, text=True,
                            timeout=TIMEOUT_COMPRESSAO)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return output_path
        print(f"[Compactacao] Tentativa 1 falhou: rc={proc.returncode}, "
              f"stderr={(proc.stderr or '')[-200:]}")
    except subprocess.TimeoutExpired:
        print(f"[Compactacao] Tentativa 1 timeout apos {TIMEOUT_COMPRESSAO}s")
    except Exception as e:
        print(f"[Compactacao] Tentativa 1 erro: {e}")

    # --- Tentativa 2: sem skip silencio ---
    cmd_sem_skip = [
        ffmpeg_exe, "-y", "-loglevel", "error",
        "-threads", "0",
        "-i", input_path,
        "-vn", "-ac", "1", "-ar", "12000",
        "-af", f"atempo={SPEED_FACTOR}",
        "-c:a", "libopus", "-b:a", "24k",
        "-application", "voip",
        output_path,
    ]
    try:
        print(f"[Compactacao] Tentativa 2: so speed {SPEED_FACTOR}x (sem skip silencio)")
        proc = subprocess.run(cmd_sem_skip, capture_output=True, text=True,
                            timeout=TIMEOUT_COMPRESSAO)
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return output_path
        print(f"[Compactacao] Tentativa 2 falhou: rc={proc.returncode}")
    except Exception as e:
        print(f"[Compactacao] Tentativa 2 erro: {e}")

    # --- Tentativa 3: AAC simples ---
    output_path_aac = str(output_dir / "compressed.m4a")
    cmd_aac = [
        ffmpeg_exe, "-y", "-loglevel", "error",
        "-threads", "0",
        "-i", input_path,
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "aac", "-b:a", "32k",
        output_path_aac,
    ]
    try:
        print(f"[Compactacao] Tentativa 3: AAC fallback")
        proc = subprocess.run(cmd_aac, capture_output=True, text=True,
                            timeout=TIMEOUT_COMPRESSAO)
        if proc.returncode == 0 and os.path.exists(output_path_aac) and os.path.getsize(output_path_aac) > 1000:
            return output_path_aac
        print(f"[Compactacao] Tentativa 3 falhou: rc={proc.returncode}")
    except Exception as e:
        print(f"[Compactacao] Tentativa 3 erro: {e}")

    return None


# ============================================================
# UPLOAD GEMINI COM POLLING DE ESTADO
# ============================================================

def _aguardar_upload_pronto(audio_file, timeout=TIMEOUT_UPLOAD):
    """Espera o Gemini terminar de processar o upload (PROCESSING -> ACTIVE)."""
    start = time.time()
    last_log = 0
    while audio_file.state.name == "PROCESSING":
        elapsed = time.time() - start
        if elapsed > timeout:
            raise RuntimeError(
                f"Upload travou em PROCESSING apos {timeout}s. Tente arquivo menor."
            )
        if int(elapsed) - last_log >= 5:
            print(f"[Transcricao] Aguardando Gemini processar upload... ({int(elapsed)}s)")
            last_log = int(elapsed)
        time.sleep(2)
        audio_file = genai.get_file(audio_file.name)

    if audio_file.state.name == "FAILED":
        raise RuntimeError(f"Upload falhou no Gemini. Estado: {audio_file.state.name}")

    if audio_file.state.name != "ACTIVE":
        raise RuntimeError(f"Estado inesperado do upload: {audio_file.state.name}")

    return audio_file


# ============================================================
# FUNCAO PRINCIPAL
# ============================================================

def transcrever(audio_path: str) -> str:
    """Transcreve audio: compacta com ffmpeg quando possivel, senao manda original."""
    t_total = time.time()
    size_mb_original = os.path.getsize(audio_path) / 1024 / 1024
    print(f"[Transcricao] === INICIO === arquivo {size_mb_original:.1f}MB")

    audio_para_enviar = audio_path
    tmpdir_obj = None

    # === ETAPA 0: Compactacao (com fallback total) ===
    if size_mb_original <= SKIP_COMPRESSION_BELOW_MB:
        print(f"[Transcricao] Arquivo pequeno ({size_mb_original:.1f}MB <= "
              f"{SKIP_COMPRESSION_BELOW_MB}MB), pulando compactacao")
    else:
        ffmpeg_exe = _achar_ffmpeg()
        if not ffmpeg_exe:
            print(f"[Transcricao] ffmpeg nao disponivel, usando audio original")
        else:
            print(f"[Transcricao] ffmpeg encontrado: {ffmpeg_exe}")
            t_comp = time.time()
            try:
                tmpdir_obj = tempfile.TemporaryDirectory(prefix="studyai_audio_")
                comprimido = _compactar_audio(ffmpeg_exe, audio_path, Path(tmpdir_obj.name))
                if comprimido and os.path.exists(comprimido):
                    novo_mb = os.path.getsize(comprimido) / 1024 / 1024
                    reducao_pct = (1 - novo_mb / size_mb_original) * 100
                    print(f"[Transcricao] Compactacao OK em {time.time()-t_comp:.1f}s: "
                          f"{size_mb_original:.1f}MB -> {novo_mb:.1f}MB "
                          f"(-{reducao_pct:.0f}%)")
                    audio_para_enviar = comprimido
                else:
                    print(f"[Transcricao] Compactacao falhou totalmente, "
                          f"usando audio original")
            except Exception as e:
                print(f"[Transcricao] Erro inesperado na compactacao: {e}, "
                      f"usando audio original")

    # === ETAPA 1: Upload ===
    audio_file = None
    try:
        upload_size_mb = os.path.getsize(audio_para_enviar) / 1024 / 1024
        print(f"[Transcricao] Fazendo upload de {upload_size_mb:.1f}MB para o Gemini...")
        t_up = time.time()
        audio_file = genai.upload_file(path=audio_para_enviar)
        print(f"[Transcricao] Upload enviado em {time.time()-t_up:.1f}s, "
              f"estado inicial: {audio_file.state.name}")

        # === ETAPA 2: Aguardar processamento ===
        audio_file = _aguardar_upload_pronto(audio_file)
        print(f"[Transcricao] Upload pronto (ACTIVE) em {time.time()-t_up:.1f}s total")

        # === ETAPA 3: Transcricao ===
        print(f"[Transcricao] Iniciando transcricao (timeout {TIMEOUT_GEMINI}s)...")
        t_tr = time.time()
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(
            [
                "Transcreva este audio completamente em portugues brasileiro. "
                "Retorne APENAS o texto transcrito, palavra por palavra, sem comentarios, "
                "sem timestamps, sem formatacao extra. Use [inaudivel] para trechos "
                "incompreensiveis. Mantenha termos tecnicos e medicos exatamente como "
                "foram pronunciados.",
                audio_file,
            ],
            request_options={"timeout": TIMEOUT_GEMINI},
            generation_config={
                "temperature": 0.1,
                "max_output_tokens": 32000,
            },
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
        # Limpa arquivo do Gemini
        if audio_file is not None:
            try:
                genai.delete_file(audio_file.name)
            except Exception:
                pass
        # Limpa pasta temporaria local
        if tmpdir_obj is not None:
            try:
                tmpdir_obj.cleanup()
            except Exception:
                pass
