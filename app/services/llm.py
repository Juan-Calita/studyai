import json
import re
import time
import google.generativeai as genai
from google.api_core import exceptions
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)
_model = genai.GenerativeModel("gemini-flash-latest")


def _parse_json(texto: str):
    """Extrai JSON robustamente, mesmo com lixo antes/depois."""
    # Tenta achar JSON em ```json ... ```
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', texto, re.DOTALL)
    if match:
        texto = match.group(1)
    else:
        # Tenta achar primeiro { ou [ até último } ou ]
        first = min(
            (texto.find(c) for c in '{[' if texto.find(c) >= 0),
            default=-1
        )
        if first >= 0:
            last_obj = texto.rfind('}')
            last_arr = texto.rfind(']')
            last = max(last_obj, last_arr)
            if last > first:
                texto = texto[first:last + 1]

    try:
        return json.loads(texto.strip(), strict=False)
    except json.JSONDecodeError as e:
        # Tenta reparar JSON cortado: fechar chaves/colchetes que faltam
        texto = texto.strip()
        opens = texto.count('{') - texto.count('}')
        opens_arr = texto.count('[') - texto.count(']')
        if opens > 0:
            texto += '}' * opens
        if opens_arr > 0:
            texto += ']' * opens_arr
        try:
            return json.loads(texto, strict=False)
        except Exception:
            raise e


def _call_with_retry(prompt, retries=2, timeout_sec=180):
    last_err = None
    for attempt in range(retries + 1):
        try:
            return _model.generate_content(
                prompt,
                request_options={"timeout": timeout_sec},
            )
        except exceptions.ResourceExhausted as e:
            last_err = e
            if attempt < retries:
                time.sleep(40)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    raise last_err


def gerar_estrutura(transcricao_bruta: str) -> dict:
    """CHAMADA A: título + resumo + transcrição estruturada (rápida)."""
    prompt = f"""Você é um assistente pedagógico. Analise a transcrição da aula e gere JSON com:

1. **titulo_sugerido**: título acadêmico curto (max 80 chars)
2. **resumo_expandido**: resumo didático e detalhado em ~1500 palavras, com ## (subseções) e ### (detalhes). Cubra TODOS os pontos importantes da aula.
3. **transcricao_destrinchada**: conteúdo reescrito sem muletas, em markdown (# ## ###). Sem caracteres ornamentais.

Responda APENAS com JSON válido:
{{"titulo_sugerido":"...","resumo_expandido":"...","transcricao_destrinchada":"..."}}

Transcrição:
{transcricao_bruta}"""
    resp = _call_with_retry(prompt, timeout_sec=180)
    return _parse_json(resp.text)


def gerar_flashcards(transcricao_bruta: str) -> list:
    """CHAMADA B: 25 flashcards (rápida, em paralelo com a A)."""
    prompt = f"""Crie 25 flashcards de estudo a partir da aula abaixo.

Regras:
- Perguntas claras e específicas (evite perguntas genéricas tipo "o que é X?")
- Respostas concisas (1-3 frases, máximo 60 palavras)
- Foque em conceitos, definições, processos e aplicações práticas
- Distribua os cards por todo o conteúdo da aula

Responda APENAS com JSON válido neste formato exato:
[{{"pergunta":"...","resposta":"..."}},{{"pergunta":"...","resposta":"..."}}]

Aula:
{transcricao_bruta[:12000]}"""
    resp = _call_with_retry(prompt, timeout_sec=120)
    return _parse_json(resp.text)


def gerar_extras(transcricao_bruta: str) -> dict:
    """CHAMADA C (opcional): guia + palácio mental.
    Roda só se A e B funcionaram. Se falhar, ignora.
    """
    prompt = f"""Crie material complementar para a aula abaixo:

1. **guia_de_estudos**: passo-a-passo prático em ~400 palavras, com metas objetivas
2. **palacio_mental**: narrativa de memorização em ~600 palavras, usando metáfora de jornada por um edifício (cômodos, salas, detalhes). Inclua frases-gatilho.

Responda APENAS com JSON válido:
{{"guia_de_estudos":"...","palacio_mental":"..."}}

Aula:
{transcricao_bruta[:8000]}"""
    resp = _call_with_retry(prompt, timeout_sec=120, retries=1)
    return _parse_json(resp.text)
