"""
Transcrição com chunking paralelo.

Estratégia:
  1. Detecta duração/tamanho do áudio.
  2. Se exceder o limite, divide em chunks com sobreposição de 2s (anti-corte de palavras).
  3. Transcreve chunks em paralelo (ThreadPoolExecutor) respeitando rate-limit.
  4. Junta as transcrições na ordem original dos chunks.
  5. Sempre limpa arquivos temporários e uploads remotos.

Fallback: se ffmpeg não estiver disponível OU o áudio couber no limite, usa upload único.
"""
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

# ---------------------------------------------------------------------------
# Parâmetros de divisão
# ---------------------------------------------------------------------------
# Duração máxima (segundos) por chunk. 15 min é um sweet-spot: cabe bem no
# contexto do Gemini Flash, sobe rápido e reduz risco de timeout/erro de upload.
CHUNK_DURATION_SEC = 15 * 60          # 15 minutos
# Sobreposição entre chunks para evitar cortar uma palavra no meio.
CHUNK_OVERLAP_SEC = 2
# Tamanho a partir do qual consideramos dividir (bytes). Abaixo disso, upload único.
SIZE_THRESHOLD_BYTES = 40 * 1024 * 1024   # 40 MB
# Duração a partir da qual consideramos dividir (segundos).
DURATION_THRESHOLD_SEC = CHUNK_DURATION_SEC + 60  # > 16 min => divide
# Workers paralelos. Gemini Flash free tier aceita ~10 RPM; 4 é conservador e rápido.
MAX_PARALLEL_WORKERS = 4
# Tentativas por chunk em caso de falha transitória.
MAX_RETRIES = 3
# Modelo usado.
MODEL_NAME = "gemini-flash-latest"


# ---------------------------------------------------------------------------
# Utilidades ffmpeg
# ---------------------------------------------------------------------------
def _get_ffmpeg_exe() -> Optional[str]:
    """Retorna o caminho do binário ffmpeg. Prioriza imageio-ffmpeg (bundled)."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    # Fallback: ffmpeg no PATH do sistema.
    return shutil.which("ffmpeg")


def _probe_duration(ffmpeg_exe: str, audio_path: str) -> Optional[float]:
    """Descobre a duração do áudio em segundos via ffmpeg (sem ffprobe)."""
    try:
        # ffmpeg -i <file> imprime metadata em stderr. Cheap e funciona.
        result = subprocess.run(
            [ffmpeg_exe, "-i", audio_path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        # Procura "Duration: HH:MM:SS.xx"
        for line in result.stderr.splitlines():
            line = line.strip()
            if line.startswith("Duration:"):
                parte = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = parte.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception as e:
        print(f"[Transcrição] Probe de duração falhou: {e}")
    return None


def _split_audio(
    ffmpeg_exe: str,
    audio_path: str,
    out_dir: Path,
    duration_sec: float,
) -> List[Tuple[int, str]]:
    """Divide o áudio em chunks de CHUNK_DURATION_SEC + overlap.

    Retorna lista de (índice, caminho_do_chunk) na ordem cronológica.
    Re-encoda para M4A (AAC mono 16kHz 64kbps) — leve, universal e preserva fala.
    """
    chunks: List[Tuple[int, str]] = []
    idx = 0
    start = 0.0
    while start < duration_sec:
        out_path = str(out_dir / f"chunk_{idx:03d}.m4a")
        # -ss antes do -i => seek rápido. -t => duração do segmento.
        # Pequena sobreposição: cada chunk cobre [start, start + CHUNK + overlap]
        # (exceto o último). O overlap deixa o modelo ouvir o fim da palavra cortada.
        seg_duration = CHUNK_DURATION_SEC + CHUNK_OVERLAP_SEC
        cmd = [
            ffmpeg_exe,
            "-y",                       # sobrescreve
            "-loglevel", "error",
            "-ss", f"{start:.3f}",
            "-i", audio_path,
            "-t", f"{seg_duration:.3f}",
            "-vn",                      # ignora vídeo (caso input seja mp4)
            "-ac", "1",                 # mono: reduz tamanho, fala não perde nada
            "-ar", "16000",             # 16 kHz é suficiente para voz
            "-c:a", "aac",
            "-b:a", "64k",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg falhou no chunk {idx}: {proc.stderr[-500:]}"
            )
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            # Segmento fora do áudio real; encerra.
            break
        chunks.append((idx, out_path))
        idx += 1
        start += CHUNK_DURATION_SEC  # avança sem overlap no ponteiro
    return chunks


# ---------------------------------------------------------------------------
# Transcrição de um chunk (com retry)
# ---------------------------------------------------------------------------
_TRANSCRIBE_PROMPT = (
    "Transcreva este áudio completamente em português. "
    "Retorne APENAS o texto transcrito, sem comentários, sem timestamps, "
    "sem formatação extra, sem cabeçalhos."
)


def _transcribe_single(audio_path: str, label: str = "") -> str:
    """Transcreve um arquivo de áudio via Gemini com retry exponencial."""
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        audio_file = None
        try:
            audio_file = genai.upload_file(path=audio_path)
            model = genai.GenerativeModel(MODEL_NAME)
            response = model.generate_content([_TRANSCRIBE_PROMPT, audio_file])
            text = (response.text or "").strip()
            print(
                f"[Transcrição] {label} ok "
                f"(tentativa {attempt}, {len(text)} chars)"
            )
            return text
        except Exception as e:
            last_err = e
            print(
                f"[Transcrição] {label} falhou (tentativa {attempt}/"
                f"{MAX_RETRIES}): {e}"
            )
            time.sleep(2 ** attempt)  # 2, 4, 8s
        finally:
            if audio_file is not None:
                try:
                    audio_file.delete()
                except Exception:
                    pass
    raise RuntimeError(f"Chunk {label} não transcrito: {last_err}")


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def transcrever(audio_path: str) -> str:
    """Transcreve áudio (com divisão paralela se necessário)."""
    size_bytes = os.path.getsize(audio_path)
    ffmpeg_exe = _get_ffmpeg_exe()

    # --- Caminho rápido: ffmpeg indisponível ----------------------------
    if ffmpeg_exe is None:
        print("[Transcrição] ffmpeg indisponível — upload único.")
        return _transcribe_single(audio_path, label="full")

    duration = _probe_duration(ffmpeg_exe, audio_path)

    precisa_dividir = (
        size_bytes > SIZE_THRESHOLD_BYTES
        or (duration is not None and duration > DURATION_THRESHOLD_SEC)
    )

    if not precisa_dividir:
        dur_txt = f"{duration/60:.1f} min" if duration else "?"
        print(
            f"[Transcrição] Arquivo cabe no limite "
            f"({size_bytes/1024/1024:.1f} MB, {dur_txt}) — upload único."
        )
        return _transcribe_single(audio_path, label="full")

    # --- Caminho dividido: chunking + paralelo --------------------------
    if duration is None:
        # Sem duração confiável: estimamos pela bitrate média ~64kbps.
        duration = size_bytes * 8 / 64_000
        print(f"[Transcrição] Duração estimada: {duration/60:.1f} min")

    print(
        f"[Transcrição] Dividindo áudio "
        f"({size_bytes/1024/1024:.1f} MB, {duration/60:.1f} min) em chunks..."
    )

    with tempfile.TemporaryDirectory(prefix="studyai_chunks_") as tmpdir:
        tmp_path = Path(tmpdir)
        try:
            chunks = _split_audio(ffmpeg_exe, audio_path, tmp_path, duration)
        except Exception as e:
            print(f"[Transcrição] Divisão falhou ({e}) — fallback para upload único.")
            return _transcribe_single(audio_path, label="full-fallback")

        if not chunks:
            print("[Transcrição] Nenhum chunk gerado — fallback para upload único.")
            return _transcribe_single(audio_path, label="full-fallback")

        print(
            f"[Transcrição] {len(chunks)} chunks gerados. "
            f"Transcrevendo em paralelo ({MAX_PARALLEL_WORKERS} workers)..."
        )

        # Executa em paralelo preservando a ordem pelo índice.
        resultados: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS) as ex:
            futuros = {
                ex.submit(_transcribe_single, path, f"chunk_{idx:03d}"): idx
                for idx, path in chunks
            }
            for fut in as_completed(futuros):
                idx = futuros[fut]
                resultados[idx] = fut.result()  # propaga erro se falhar após retries

        # Junta na ordem cronológica.
        texto_final = "\n\n".join(
            resultados[i] for i in sorted(resultados.keys()) if resultados[i]
        ).strip()

    print(f"[Transcrição] Concluída ({len(texto_final)} caracteres).")
    return texto_final
