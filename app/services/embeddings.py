import google.generativeai as genai
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)

MODEL = "models/text-embedding-004"


def embed_one(texto: str) -> list[float]:
    result = genai.embed_content(model=MODEL, content=texto)
    return result["embedding"]


def embed(textos: list[str]) -> list[list[float]]:
    embeddings = []
    # Gemini embed aceita um texto por vez
    for t in textos:
        result = genai.embed_content(model=MODEL, content=t)
        embeddings.append(result["embedding"])
    return embeddings
