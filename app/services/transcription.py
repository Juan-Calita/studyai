"""Transcricao: so compacta se arquivo > 90MB. Caso contrario sobe original direto."""
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

MODEL_NAME = "gemini-2.5-pro"
MODEL_FALLBACK = "gemini-2.5-flash"
TIMEOUT_GEMINI = 300              # 5 min para Gemini transcrever
TIMEOUT_UPLOAD = 180              # 3 min para upload terminar de processar
TIMEOUT_COMPRESSAO = 90           # 90s no MAX para compactar
SKIP_COMPRESSION_BELOW_MB = 2.0   # Compacta tudo acima de 2MB
SPEED_FACTOR = 1.4                # equilibrio: ganho real sem perder termos medicos


# ============================================================
# UPLOAD GEMINI COM POLLING DE ESTADO
# ============================================================

def _aguardar_upload_pronto(audio_file, timeout=TIMEOUT_UPLOAD):
    """Espera o Gemini terminar de processar o upload."""
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
# COMPACTACAO COM FFMPEG (so para arquivos > 90MB)
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
    """Compactacao simples e rapida: speed 1.4x + Opus 24kbps mono.
    SEM silenceremove (que e o filtro lento)."""
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
    """Resultado compartilhado entre thread de compactacao e main."""
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
    """Transcreve audio. So tenta compactar se passar de 90MB."""
    t_total = time.time()
    size_mb_original = os.path.getsize(audio_path) / 1024 / 1024
    print(f"[Transcricao] === INICIO === arquivo {size_mb_original:.1f}MB")

    audio_para_enviar = audio_path
    tmpdir_obj = None
    compact_thread = None
    compact_result = None
    audio_file = None

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

        # === ETAPA 4: Upload ao Gemini ===
        upload_size_mb = os.path.getsize(audio_para_enviar) / 1024 / 1024
        print(f"[Transcricao] Fazendo upload de {upload_size_mb:.1f}MB ao Gemini...")
        t_up = time.time()
        audio_file = genai.upload_file(path=audio_para_enviar)
        print(f"[Transcricao] Upload enviado em {time.time()-t_up:.1f}s, "
              f"estado inicial: {audio_file.state.name}")

        # === ETAPA 5: Aguarda processamento do upload ===
        audio_file = _aguardar_upload_pronto(audio_file)
        print(f"[Transcricao] Upload pronto (ACTIVE) em {time.time()-t_up:.1f}s total")

        # === ETAPA 6: Transcricao (com fallback de modelo) ===
        PROMPT_TRANSCRICAO = (
            "Voce e um transcritor especializado em aulas academicas em portugues brasileiro. "
            "Transcreva COMPLETAMENTE este audio, palavra por palavra, sem omitir nada. "
            "REGRAS OBRIGATORIAS: "
            "1. Retorne APENAS o texto transcrito - zero comentarios, zero timestamps, zero explicacoes. "
            "2. Use [inaudivel] para trechos incompreensiveis. "
            "3. Preserve TODOS os termos tecnicos, medicos e cientificos exatamente como pronunciados. "
            "4. Separe em paragrafos curtos por mudanca de topico ou raciocinio. "
            "5. NAO resuma, NAO omita partes, transcreva absolutamente tudo. "
            "6. Corrija apenas erros gramaticais obvios, mantenha o conteudo intacto."
        )
        GEN_CONFIG = {"temperature": 0.0, "max_output_tokens": 32000}

        text = ""
        for model_name in (MODEL_NAME, MODEL_FALLBACK):
            print(f"[Transcricao] Tentando modelo {model_name} (timeout {TIMEOUT_GEMINI}s)...")
            t_tr = time.time()
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(
                    [PROMPT_TRANSCRICAO, audio_file],
                    request_options={"timeout": TIMEOUT_GEMINI},
                    generation_config=GEN_CONFIG,
                )
                text = (response.text or "").strip()
                print(f"[Transcricao] {model_name} respondeu em {time.time()-t_tr:.1f}s, "
                      f"{len(text)} chars")
                if text and len(text) >= 30:
                    break
                print(f"[Transcricao] {model_name} retornou texto curto ({len(text)} chars), tentando fallback...")
            except Exception as e:
                print(f"[Transcricao] {model_name} falhou: {type(e).__name__}: {e}, tentando fallback...")

        if not text or len(text) < 30:
            raise RuntimeError(f"Transcricao vazia apos todos os modelos ({len(text)} chars)")

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
        # Limpa pasta temporaria
        if tmpdir_obj is not None:
            try:
                tmpdir_obj.cleanup()
            except Exception:
                pass
