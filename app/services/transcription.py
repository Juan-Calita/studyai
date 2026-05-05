"""Transcricao simples: upload direto, 1 tentativa, sem chunking."""
from __future__ import annotations
import os
import time

import google.generativeai as genai
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)

MODEL_NAME = "gemini-flash-latest"
TIMEOUT_SEC = 600


def transcrever(audio_path: str) -> str:
    """Upload direto + 1 tentativa de transcricao."""
    size_mb = os.path.getsize(audio_path) / 1024 / 1024
    print(f"[Transcricao] Arquivo: {audio_path} ({size_mb:.1f}MB)")

    audio_file = None
    try:
        print(f"[Transcricao] Fazendo upload...")
        t0 = time.time()
        audio_file = genai.upload_file(path=audio_path)
        print(f"[Transcricao] Upload iniciado em {time.time()-t0:.1f}s. Aguardando processamento...")

        # Aguarda o Gemini processar o upload
        wait_count = 0
        while audio_file.state.name == "PROCESSING":
            time.sleep(3)
            wait_count += 1
            if wait_count > 60:  # 3 min max
                raise RuntimeError("Upload demorou demais para processar")
            audio_file = genai.get_file(audio_file.name)
            if wait_count % 5 == 0:
                print(f"[Transcricao] ...ainda processando ({wait_count*3}s)")

        if audio_file.state.name == "FAILED":
            raise RuntimeError(f"Upload falhou no Gemini")

        print(f"[Transcricao] Upload completo em {time.time()-t0:.1f}s. Pedindo transcricao...")
        t1 = time.time()
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(
            ["Transcreva este audio em portugues. Retorne APENAS o texto, sem comentarios, sem timestamps.", audio_file],
            request_options={"timeout": TIMEOUT_SEC},
        )
        text = (response.text or "").strip()
        print(f"[Transcricao] Concluida ({len(text)} chars em {time.time()-t1:.1f}s)")
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
