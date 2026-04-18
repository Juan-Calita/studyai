import json
import re
import time
import google.generativeai as genai
from google.api_core import exceptions
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)
_model = genai.GenerativeModel("gemini-flash-latest")


def _parse_json(texto: str):
    match = re.search(r'(\{.*\}|\[.*\])', texto, re.DOTALL)
    if not match:
        match = re.search(r'```(?:json)?\s*(.*?)\s*```', texto, re.DOTALL)
        texto = match.group(1) if match else texto
    else:
        texto = match.group(0)
    return json.loads(texto.strip(), strict=False)


def _call_with_retry(prompt, retries=1):
    try:
        return _model.generate_content(prompt)
    except exceptions.ResourceExhausted:
        if retries > 0:
            time.sleep(40)
            return _call_with_retry(prompt, retries - 1)
        raise


def processar_tudo(transcricao_bruta: str) -> dict:
    """Chamada única que gera TUDO: estruturação + deep dive + 30 flashcards."""
    prompt = f"""Você é um assistente pedagógico. Analise a transcrição da aula abaixo e gere UM ÚNICO JSON com TODOS estes campos:

1. **titulo_sugerido**: título acadêmico formal.
2. **resumo_expandido**: Mínimo de 4 páginas de texto denso. Use ## e ### (markdown). Minucioso, cobrindo TODO detalhe técnico. Didático.
3. **guia_de_estudos**: Passo a passo prático com metas objetivas para dominar a matéria.
4. **palacio_mental**: Narrativa de jornada por um edifício, do básico ao avançado. Use # (cômodos), ## (salas), ### (detalhes). Analogias e frases-gatilho de memorização.
5. **transcricao_destrinchada**: 100% do conteúdo técnico reescrito sem muletas, estruturado com markdown (# ## ###). Elimine caracteres no meio do texto (* - @ $).
6. **analise_de_enfase**: O que o professor mais enfatizou e repetiu (termos como "importante", "essencial", "cuidado", "não esqueçam").
7. **flashcards**: Mínimo de 30 flashcards EXTRAÍDOS DIRETAMENTE da transcrição bruta. Foco em detalhes técnicos e pontos repetidos pelo professor. Perguntas claras e específicas, respostas concisas (1-3 frases).

Responda EXCLUSIVAMENTE com JSON válido:
{{
  "titulo_sugerido": "...",
  "resumo_expandido": "...",
  "guia_de_estudos": "...",
  "palacio_mental": "...",
  "transcricao_destrinchada": "...",
  "analise_de_enfase": "...",
  "flashcards": [{{"pergunta": "...", "resposta": "..."}}]
}}

Transcrição:
{transcricao_bruta}"""

    resp = _call_with_retry(prompt)
    return _parse_json(resp.text)
