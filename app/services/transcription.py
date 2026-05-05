"""Transcricao simples via Gemini (sem ffmpeg, sem compressao)."""
import google.generativeai as genai
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)


def transcrever(audio_path: str) -> str:
    """Transcreve audio usando Gemini diretamente."""
    print(f"[Transcricao] Enviando audio para Gemini...")
    audio_file = genai.upload_file(path=audio_path)

    model = genai.GenerativeModel("gemini-flash-latest")
    response = model.generate_content([
        "Transcreva este audio completamente em portugues brasileiro. "
        "Retorne APENAS o texto transcrito, palavra por palavra, sem comentarios, "
        "sem timestamps, sem formatacao extra. Use [inaudivel] para trechos incompreensiveis.",
        audio_file,
    ])

    # Limpa o arquivo do servidor do Google
    try:
        genai.delete_file(audio_file.name)
    except Exception:
        pass

    text = (response.text or "").strip()
    print(f"[Transcricao] Concluida ({len(text)} caracteres)")

    if not text or len(text) < 30:
        raise RuntimeError(f"Resposta vazia ou muito curta ({len(text)} chars)")

    return text
