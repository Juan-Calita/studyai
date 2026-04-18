import google.generativeai as genai
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)


def transcrever(audio_path: str) -> str:
    """Transcreve áudio usando Gemini 1.5 Flash (aceita áudio direto)."""
    print(f"[Transcrição] Enviando áudio para Gemini...")
    audio_file = genai.upload_file(path=audio_path)

   model = genai.GenerativeModel("gemini-2.0-flash")
    response = model.generate_content([
        "Transcreva este áudio completamente em português. "
        "Retorne APENAS o texto transcrito, sem comentários, sem timestamps, sem formatação extra.",
        audio_file,
    ])

    # Limpa o arquivo do servidor do Google
    try:
        audio_file.delete()
    except Exception:
        pass

    print(f"[Transcrição] Concluída ({len(response.text)} caracteres)")
    return response.text
