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
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', texto, re.DOTALL)
    if match:
        texto = match.group(1)
    else:
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
    """CHAMADA A: titulo + resumo + transcricao estruturada."""
    prompt = f"""Voce e um assistente pedagogico. Analise a transcricao da aula e gere JSON com:

1. **titulo_sugerido**: titulo academico curto (max 80 chars)
2. **resumo_expandido**: resumo didatico e detalhado em ~1500 palavras, com ## (subsecoes) e ### (detalhes). Cubra TODOS os pontos importantes da aula.
3. **transcricao_destrinchada**: conteudo reescrito sem muletas, em markdown (# ## ###). Sem caracteres ornamentais.

Responda APENAS com JSON valido:
{{"titulo_sugerido":"...","resumo_expandido":"...","transcricao_destrinchada":"..."}}

Transcricao:
{transcricao_bruta}"""
    resp = _call_with_retry(prompt, timeout_sec=180)
    return _parse_json(resp.text)


def gerar_flashcards(transcricao_bruta: str) -> list:
    """CHAMADA B: 25 flashcards."""
    prompt = f"""Crie 25 flashcards de estudo a partir da aula abaixo.

Regras:
- Perguntas claras e especificas (evite perguntas genericas tipo "o que e X?")
- Respostas concisas (1-3 frases, maximo 60 palavras)
- Foque em conceitos, definicoes, processos e aplicacoes praticas
- Distribua os cards por todo o conteudo da aula

Responda APENAS com JSON valido neste formato exato:
[{{"pergunta":"...","resposta":"..."}},{{"pergunta":"...","resposta":"..."}}]

Aula:
{transcricao_bruta[:12000]}"""
    resp = _call_with_retry(prompt, timeout_sec=120)
    return _parse_json(resp.text)


def gerar_extras(transcricao_bruta: str) -> dict:
    """CHAMADA C (opcional): guia de estudos completo (programa + bibliografia) + palacio mental."""
    prompt = f"""Analise o tema da aula abaixo e gere material complementar de estudo aprofundado.

VOCE DEVE GERAR DOIS CAMPOS:

==========================================
CAMPO 1: guia_de_estudos
==========================================
O guia_de_estudos deve ter DUAS PARTES bem separadas em markdown:

## PARTE 1 - CONTEUDO PROGRAMATICO COMPLETO

Crie um conteudo programatico completo para aprender o tema da aula, do nivel iniciante ao avancado.
Organize por modulos. Para cada modulo:
- Liste os topicos mais importantes em ordem de prioridade
- Destaque quais sao os fundamentos essenciais que todo iniciante deve dominar primeiro

Use esta estrutura para CADA modulo:

### Modulo 1: [Nome do modulo] (Nivel: Iniciante)
**Fundamentos essenciais (dominar primeiro):**
- Topico 1 (mais prioritario)
- Topico 2
- Topico 3

**Topicos complementares:**
- Topico 4
- Topico 5

### Modulo 2: [Nome do modulo] (Nivel: Intermediario)
[mesma estrutura]

### Modulo 3: [Nome do modulo] (Nivel: Avancado)
[mesma estrutura]

Gere de 4 a 6 modulos no total, progredindo de iniciante a avancado.

## PARTE 2 - BIBLIOGRAFIA RECOMENDADA POR MODULO

Para CADA modulo da Parte 1, indique de 1 a 5 livros ou artigos cientificos confiaveis.
Prefira materiais em portugues, mas aceite ingles se for referencia fundamental na area.
Para cada indicacao, explique em 2 linhas por que ela e relevante para quem esta comecando.

Use esta estrutura:

### Bibliografia - Modulo 1: [Nome]
1. **[Titulo do livro/artigo]** - [Autor(es)], [Ano]
   *Por que ler:* [explicacao em 2 linhas sobre relevancia para iniciantes]

2. **[Titulo do livro/artigo]** - [Autor(es)], [Ano]
   *Por que ler:* [explicacao em 2 linhas]

### Bibliografia - Modulo 2: [Nome]
[mesma estrutura, 1 a 5 indicacoes]

[continuar para todos os modulos]

==========================================
CAMPO 2: palacio_mental
==========================================
Narrativa de memorizacao em ~600 palavras, usando metafora de jornada por um edificio (comodos, salas, detalhes). Inclua frases-gatilho de memorizacao.

==========================================
RESPOSTA
==========================================
Responda APENAS com JSON valido neste formato exato:
{{"guia_de_estudos":"...","palacio_mental":"..."}}

Use \\n para quebras de linha dentro das strings.
NAO use aspas duplas dentro do conteudo (use aspas simples se precisar).

Aula:
{transcricao_bruta[:8000]}"""
    resp = _call_with_retry(prompt, timeout_sec=180, retries=1)
    return _parse_json(resp.text)
