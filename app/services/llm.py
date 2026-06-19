"""LLM service: 4 funcoes isoladas. Flashcards adaptativos com parsing robusto."""
import json
import re
import time
import google.generativeai as genai
from google.api_core import exceptions
from app.config import settings

genai.configure(api_key=settings.gemini_api_key)
_model = genai.GenerativeModel("gemini-flash-latest")


# ============================================================
# UTILS
# ============================================================

def _parse_json(texto: str):
    """Extrai JSON robustamente, com auto-repair."""
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', texto, re.DOTALL)
    if match:
        texto = match.group(1)
    else:
        first = -1
        for c in '{[':
            idx = texto.find(c)
            if idx >= 0 and (first < 0 or idx < first):
                first = idx
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


def _extrair_cards_robustamente(texto: str) -> list:
    """Extrai flashcards mesmo se JSON estiver malformado/cortado.
    Aceita varios formatos e tenta recuperar ate cards individuais."""
    # Tenta JSON primeiro
    try:
        data = _parse_json(texto)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # As vezes vem encapsulado: {"flashcards": [...]} ou {"cards": [...]}
            for key in ('flashcards', 'cards', 'items', 'data', 'list'):
                if key in data and isinstance(data[key], list):
                    return data[key]
    except Exception:
        pass

    # Fallback: extrai cards individuais via regex (cada {"pergunta":"...","resposta":"..."})
    cards = []
    # Padrao mais permissivo que aceita pergunta/question/front e resposta/answer/back
    padrao = re.compile(
        r'\{[^{}]*?"(?:pergunta|question|front|q)"\s*:\s*"((?:[^"\\]|\\.)*)"'
        r'[^{}]*?"(?:resposta|answer|back|a)"\s*:\s*"((?:[^"\\]|\\.)*)"[^{}]*?\}',
        re.DOTALL | re.IGNORECASE
    )
    for match in padrao.finditer(texto):
        pergunta = match.group(1).replace('\\"', '"').replace('\\n', '\n').strip()
        resposta = match.group(2).replace('\\"', '"').replace('\\n', '\n').strip()
        if pergunta and resposta:
            cards.append({"pergunta": pergunta, "resposta": resposta})

    return cards


def _call_with_retry(prompt, retries=3, max_tokens=None, timeout=180):
    last_err = None
    config = {"max_output_tokens": max_tokens} if max_tokens else {}
    for attempt in range(retries + 1):
        try:
            return _model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(**config) if config else None,
                request_options={"timeout": timeout},
            )
        except exceptions.ResourceExhausted as e:
            last_err = e
            wait = min(20 * (attempt + 1), 60)
            if attempt < retries:
                print(f"[LLM] Quota exhausted (tentativa {attempt+1}), aguardando {wait}s...")
                time.sleep(wait)
        except exceptions.DeadlineExceeded as e:
            last_err = e
            if attempt < retries:
                print(f"[LLM] Timeout na tentativa {attempt+1}, retentando...")
                time.sleep(5)
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = 5 * (attempt + 1)
                print(f"[LLM] Erro tentativa {attempt+1}: {type(e).__name__}, aguardando {wait}s...")
                time.sleep(wait)
    raise last_err


# ============================================================
# BLOCO 1: RESUMO + TITULO + TRANSCRICAO ESTRUTURADA
# ============================================================

_MAX_RESUMO_CHARS = 30000   # ~7500 tokens de entrada
_MAX_GUIA_CHARS   = 12000
_MAX_PALACE_CHARS = 14000

def gerar_resumo(transcricao_bruta: str) -> dict:
    """Gera titulo + resumo expandido. Transcricao limpa e gerada separadamente."""
    trecho = transcricao_bruta[:_MAX_RESUMO_CHARS]
    cortado = len(transcricao_bruta) > _MAX_RESUMO_CHARS

    prompt = f"""Voce e um assistente pedagogico especializado. Analise a transcricao da aula abaixo.

{"ATENCAO: a transcricao foi cortada nos primeiros " + str(_MAX_RESUMO_CHARS) + " caracteres por ser muito longa." if cortado else ""}

Gere um JSON com EXATAMENTE 2 campos:

1. "titulo_sugerido": titulo academico objetivo (max 80 chars, sem aspas duplas).
2. "resumo_expandido": resumo didatico detalhado em ~1500 palavras em markdown. Use ## para subsecoes e ### para detalhes. Cubra TODOS os topicos importantes em ordem. Use apenas aspas simples dentro do texto.

JSON de saida (comece com {{ e termine com }}, nada antes nem depois):
{{
  "titulo_sugerido": "titulo aqui",
  "resumo_expandido": "resumo completo aqui em markdown"
}}

Transcricao:
{trecho}"""

    resp = _call_with_retry(prompt, max_tokens=4096, timeout=120)
    data = _parse_json(resp.text)

    # Garante que os campos existam
    if not isinstance(data, dict):
        data = {}
    if not data.get("titulo_sugerido"):
        data["titulo_sugerido"] = ""
    if not data.get("resumo_expandido"):
        data["resumo_expandido"] = ""

    # Gera transcricao estruturada separadamente (chamada menor, mais confiavel)
    try:
        data["transcricao_destrinchada"] = _gerar_transcricao_estruturada(transcricao_bruta)
    except Exception as e:
        print(f"[LLM] Transcricao estruturada falhou: {e}, usando bruta")
        data["transcricao_destrinchada"] = transcricao_bruta

    return data


def _gerar_transcricao_estruturada(transcricao_bruta: str) -> str:
    """Reescreve a transcricao em markdown limpo, em chunks se necessario."""
    CHUNK = 20000
    if len(transcricao_bruta) <= CHUNK:
        return _estruturar_chunk(transcricao_bruta)

    # Para transcricoes longas, processa em chunks e concatena
    chunks = [transcricao_bruta[i:i+CHUNK] for i in range(0, len(transcricao_bruta), CHUNK)]
    partes = []
    for i, chunk in enumerate(chunks, 1):
        try:
            partes.append(f"## Parte {i}\n\n" + _estruturar_chunk(chunk))
        except Exception as e:
            print(f"[LLM] Chunk {i} falhou: {e}, usando bruto")
            partes.append(f"## Parte {i}\n\n" + chunk)
    return "\n\n".join(partes)


def _estruturar_chunk(texto: str) -> str:
    prompt = f"""Reescreva o texto abaixo em markdown limpo e organizado.
Mantenha 100% do conteudo tecnico. Organize com paragrafos claros.
Use # ## ### para estruturar. Remova muletas de fala (ne, tipo, assim, entao).
Retorne APENAS o markdown, sem JSON, sem explicacoes.

Texto:
{texto}"""
    resp = _call_with_retry(prompt, max_tokens=4096, timeout=90)
    return (resp.text or texto).strip()


# ============================================================
# BLOCO 2: PALACIO MENTAL
# ============================================================

def gerar_palacio_mental(transcricao_bruta: str, titulo: str = "") -> str:
    """Gera palacio mental real e funcional, com 7 regras tecnicas."""
    prompt = f"""Construa um PALACIO MENTAL real e funcional para o conteudo da aula abaixo. Siga RIGOROSAMENTE as 7 regras tecnicas:

REGRA 1 - Bloque o conteudo antes de construir
Identifique conceitos principais, detalhes secundarios e ordem logica. Se for doenca: definicao -> causa -> fisiopatologia -> clinica -> diagnostico -> tratamento -> complicacoes. Se for classificacao: cada categoria separada. Se for processo: preserve sequencia.

REGRA 2 - Escolha um lugar concreto e estavel
Use lugar especifico e visualizavel (uma casa, hospital, faculdade, igreja). NAO use ambientes vagos. Defina ROTA FIXA com pontos numerados. Cada ponto e um LOCUS.

REGRA 3 - Um locus, uma ideia principal
Nao sobrecarregue. Se ideia tem muitos detalhes, use SUB-LOCI (ex: locus = sofa; sub-loci = almofada esquerda, almofada direita, encosto, braco).

REGRA 4 - Converta abstracoes em IMAGENS CONCRETAS E ATIVAS
Conceitos viram objetos/personagens/cenas absurdas:
- Inflamacao -> fogo
- Hipertensao -> esfigmomanometro esmagando algo
- Anemia -> pessoa palida carregando hemacias vazias
- Obstrucao -> porta bloqueada
A imagem PRECISA AGIR. Use movimento, exagero, humor, estranheza, sons, cheiros. Quanto mais absurda, melhor (sem distorcer o conteudo).

REGRA 5 - Ordem da rota = ordem do conteudo
Primeiro locus = primeiro evento. Para comparacoes, dois lados do ambiente. Para criterios, objetos numerados.

REGRA 6 - Cada locus precisa de uma PISTA curta
Frase de 2-5 palavras (ex: "pressao esmagando", "fogo inflamatorio").

REGRA 7 - Tamanho ideal: 7 a 15 loci. Maximo 15.

==========================================
ESTRUTURA OBRIGATORIA (markdown puro, ~800 palavras):

# Palacio Mental: {titulo or "[tema da aula]"}

## Lugar escolhido
[2-3 frases descrevendo o local]

## Rota fixa
[Lista numerada dos 7-15 loci]

## Loci

### Locus 1: [Nome do local] - [Conceito]
**Cena:** [Imagem ATIVA com movimento. 3-5 frases.]
**Pista:** [frase curta]
**Conteudo real:** [o que representa de verdade]

### Locus 2: ... [continua para todos]

## Caminhada mental sugerida
[~150 palavras percorrendo a rota como historia]

## Treinos
- **Ordem direta:** percorra do locus 1 ao ultimo
- **Ordem inversa:** comece do ultimo
- **Ordem aleatoria:** [3 perguntas tipo "o que esta no locus X?"]

==========================================
Retorne APENAS o markdown puro (sem JSON, sem aspas envolvendo).

Transcricao da aula:
{transcricao_bruta[:_MAX_PALACE_CHARS]}"""

    resp = _call_with_retry(prompt, max_tokens=3000, timeout=120)
    return (resp.text or "").strip()


# ============================================================
# BLOCO 3: FLASHCARDS ADAPTATIVOS
# ============================================================

def gerar_flashcards(transcricao_bruta: str) -> list:
    """Gera flashcards adaptativos: 30+ cards, escala com duracao da aula."""
    # Estima duracao pelo tamanho da transcricao
    chars = len(transcricao_bruta)
    if chars < 8000:
        n_cards = 30
        max_tokens = 6000
    elif chars < 18000:
        n_cards = 40
        max_tokens = 8000
    else:
        n_cards = 50
        max_tokens = 10000

    # Limita entrada para nao explodir o contexto de saida
    MAX_INPUT = 25000
    trecho = transcricao_bruta[:MAX_INPUT]
    print(f"[Flashcards] Transcricao={chars} chars (enviando {len(trecho)}) -> alvo={n_cards} cards")

    prompt = f"""Crie EXATAMENTE {n_cards} flashcards de estudo cobrindo TODO o conteudo da aula abaixo.

REGRAS:
- Exatamente {n_cards} cards, distribuidos por todas as secoes (inicio ao fim)
- Perguntas claras e especificas; respostas concisas (max 60 palavras)
- Cubra: definicoes, valores numericos, criterios, doses, classificacoes, causas, mecanismos, sintomas, exames, tratamentos
- Sem repeticoes

FORMATO - apenas o JSON, comecando com [ e terminando com ]:
[{{"pergunta":"...","resposta":"..."}},{{"pergunta":"...","resposta":"..."}}]

Regras de formatacao:
- Aspas duplas para chaves e valores
- Dentro do conteudo, use aspas simples
- Sem quebras de linha dentro de strings
- Sem comentarios
- Termine com ] fechando todos os objetos

CONTEUDO DA AULA:
{trecho}"""

    resp = _call_with_retry(prompt, max_tokens=max_tokens, timeout=150)
    cards = _extrair_cards_robustamente(resp.text or "")
    print(f"[Flashcards] Extraidos: {len(cards)} cards")

    # Se veio menos da metade do alvo, tenta de novo com prompt simplificado
    if len(cards) < (n_cards // 2):
        print(f"[Flashcards] Poucos cards ({len(cards)}/{n_cards}), tentando fallback...")
        try:
            cards_extra = _gerar_flashcards_fallback(transcricao_bruta, n_cards - len(cards))
            cards.extend(cards_extra)
            print(f"[Flashcards] Apos fallback: {len(cards)} cards")
        except Exception as e:
            print(f"[Flashcards] Fallback falhou: {e}")

    return cards


def _gerar_flashcards_fallback(transcricao_bruta: str, alvo: int) -> list:
    """Prompt mais simples e curto, ultimo recurso quando o principal falha."""
    prompt = f"""Crie {alvo} flashcards a partir da aula abaixo.

Formato JSON:
[{{"pergunta":"P1","resposta":"R1"}},{{"pergunta":"P2","resposta":"R2"}}]

Apenas o JSON. Sem texto antes nem depois.

Aula:
{transcricao_bruta[:15000]}"""

    resp = _call_with_retry(prompt, max_tokens=6000, timeout=120)
    return _extrair_cards_robustamente(resp.text or "")


# ============================================================
# BLOCO 4: GUIA DE ESTUDOS + BIBLIOGRAFIA
# ============================================================

def gerar_guia_completo(transcricao_bruta: str) -> str:
    """Gera guia programatico + bibliografia num unico markdown."""
    prompt = f"""Analise o tema da aula abaixo e crie um GUIA DE ESTUDOS COMPLETO em markdown, com DUAS PARTES.

==========================================
PARTE 1 - CONTEUDO PROGRAMATICO COMPLETO
==========================================
4 a 6 modulos do iniciante ao avancado. Para cada modulo:

### Modulo X: [Nome] (Nivel: Iniciante / Intermediario / Avancado)

**Fundamentos essenciais (dominar primeiro):**
- Topico 1
- Topico 2

**Topicos complementares:**
- Topico 3

==========================================
PARTE 2 - BIBLIOGRAFIA POR MODULO
==========================================
Para CADA modulo, 1 a 5 livros/artigos confiaveis (preferir portugues).

### Bibliografia - Modulo X: [Nome]
1. **[Titulo]** - [Autor], [Ano]
   *Por que ler:* [2 linhas]

==========================================
Retorne APENAS markdown puro.

Aula:
{transcricao_bruta[:_MAX_GUIA_CHARS]}"""

    resp = _call_with_retry(prompt, max_tokens=3000, timeout=120)
    return (resp.text or "").strip()
