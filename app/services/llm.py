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


def estruturar_transcricao(transcricao_bruta: str) -> dict:
    prompt = f"""Você recebeu a transcrição bruta de uma aula. Sua tarefa:
1. Limpe muletas ("né", "tipo", repetições) sem alterar o conteúdo
2. Organize em seções com títulos (markdown)
3. Gere um resumo EXTREMAMENTE EXPANDIDO, MINUCIOSO, didático e detalhado de TODA a matéria, cobrindo ABSOLUTAMENTE TODOS os pontos essenciais da aula com profundidade. O resumo deve ter no mínimo 4 páginas de texto denso, estruturado em subtemas claros (## e ### em markdown).
4. Gere uma apresentação da matéria com guia de estudos passo a passo prático com metas objetivas.
5. Crie um "palácio mental" da matéria: uma narrativa de jornada por um edifício, do básico ao avançado. Use cômodos (#), salas (##) e detalhes (###). Conceitos específicos integrados em parágrafos/listas. Inclua analogias, associações visuais e frases-gatilho para memorização.

Responda EXCLUSIVAMENTE com JSON:
{{"titulo_sugerido": "...", "resumo_expandido": "...", "transcricao_estruturada": "# Seção 1\\n\\nconteúdo...", "guia_de_estudos": "...", "palacio_mental": "# Fundamentos\\n\\n## Conceitos..."}}

Transcrição:
{transcricao_bruta}"""
    resp = _call_with_retry(prompt)
    return _parse_json(resp.text)


def deep_dive(transcricao_bruta: str) -> dict:
    prompt = f"""Analise a transcrição abaixo com foco total em:
1. Pontos que o professor repetiu mais de uma vez
2. Afirmações com ênfase ("importante", "essencial", "cuidado", "não esqueçam")
3. Destrinchar 100% do conteúdo técnico em estrutura detalhada
4. Eliminar caracteres especiais no meio do texto (* - # @ $)

REGRAS PARA FLASHCARDS:
- Extraídos DIRETAMENTE da transcrição, não do resumo
- Focar em detalhes técnicos e pontos repetidos pelo professor
- Gere no mínimo 30 flashcards

Responda EXCLUSIVAMENTE com JSON:
{{"titulo_detalhado": "...", "analise_de_enfase": "...", "transcricao_destrinchada": "# Conteúdo Detalhado...", "flashcards_extensivos": [{{"pergunta": "...", "resposta": "..."}}]}}

Transcrição:
{transcricao_bruta}"""
    resp = _call_with_retry(prompt)
    return _parse_json(resp.text)
