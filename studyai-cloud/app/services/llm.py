import json
import re
import google.generativeai as genai
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)
_model = genai.GenerativeModel("gemini-1.5-flash")


def _parse_json(texto: str):
    m = re.search(r"```(?:json)?\s*(.*?)```", texto, re.DOTALL)
    if m:
        texto = m.group(1)
    return json.loads(texto.strip())


def estruturar_transcricao(transcricao_bruta: str) -> dict:
    prompt = f"""Você recebeu a transcrição bruta de uma aula. Sua tarefa:
1. Limpe muletas ("né", "tipo", repetições) sem alterar o conteúdo
2. Organize em seções com títulos (markdown)
3. Gere um resumo executivo de 5-10 linhas

Responda APENAS com JSON válido (sem texto extra):
{{"titulo_sugerido": "...", "resumo": "...", "transcricao_estruturada": "# Seção 1\\n\\nconteúdo..."}}

Transcrição:
{transcricao_bruta}"""
    resp = _model.generate_content(prompt)
    return _parse_json(resp.text)


def gerar_flashcards(transcricao: str, n: int = 15) -> list[dict]:
    prompt = f"""Crie {n} flashcards de estudo a partir da aula abaixo.
Regras: perguntas claras e específicas, respostas concisas (1-3 frases), cobrir os pontos mais importantes.

Responda APENAS com JSON válido (sem texto extra):
[{{"pergunta": "...", "resposta": "..."}}, ...]

Aula:
{transcricao}"""
    resp = _model.generate_content(prompt)
    return _parse_json(resp.text)
