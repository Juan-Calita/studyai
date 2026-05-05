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


def _call_with_retry(prompt, retries=2, max_tokens=None):
    last_err = None
    config = {}
    if max_tokens:
        config['max_output_tokens'] = max_tokens
    for attempt in range(retries + 1):
        try:
            if config:
                return _model.generate_content(
                    prompt,
                    generation_config=genai.types.GenerationConfig(**config)
                )
            return _model.generate_content(prompt)
        except exceptions.ResourceExhausted as e:
            last_err = e
            if attempt < retries:
                print(f"[LLM] Quota exhausted, aguardando 40s...")
                time.sleep(40)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    raise last_err


# ============================================================
# BLOCO 1: RESUMO + TITULO + TRANSCRICAO ESTRUTURADA
# ============================================================

def gerar_resumo(transcricao_bruta: str) -> dict:
    """Gera titulo + resumo expandido + transcricao limpa."""
    prompt = f"""Voce e um assistente pedagogico. Analise a transcricao da aula abaixo e gere JSON com 3 campos:

1. **titulo_sugerido**: titulo academico curto e objetivo (max 80 chars).

2. **resumo_expandido**: resumo didatico e detalhado em ~1500 palavras. Use markdown com ## (subsecoes) e ### (detalhes). Cubra TODOS os pontos importantes da aula.

3. **transcricao_destrinchada**: 100% do conteudo tecnico reescrito sem muletas, em markdown (# ## ###). Sem caracteres ornamentais (* - @ $).

Responda APENAS com JSON valido:
{{
  "titulo_sugerido": "...",
  "resumo_expandido": "...",
  "transcricao_destrinchada": "..."
}}

Use \\n para quebras de linha. Nao use aspas duplas dentro do conteudo (use aspas simples).

Transcricao:
{transcricao_bruta}"""

    resp = _call_with_retry(prompt)
    return _parse_json(resp.text)


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
{transcricao_bruta[:10000]}"""

    resp = _call_with_retry(prompt)
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

    print(f"[Flashcards] Transcricao={chars} chars -> alvo={n_cards} cards")

    prompt = f"""Crie EXATAMENTE {n_cards} flashcards de estudo cobrindo TODO o conteudo da aula abaixo.

REGRAS RIGOROSAS:
- Crie {n_cards} cards (nao menos)
- Distribua os cards igualmente por todas as secoes da aula (do inicio ao fim)
- Perguntas claras, especificas e diretas
- Respostas concisas (1-3 frases, max 60 palavras cada)
- INCLUA cards sobre: definicoes exatas, valores numericos, criterios formais, doses, contraindicacoes, classificacoes, comparacoes, causas, mecanismos, sintomas, exames, tratamentos
- Evite repetir o mesmo conceito em cards diferentes

FORMATO DE SAIDA - JSON puro, comecando com [ e terminando com ]:
[{{"pergunta":"...","resposta":"..."}},{{"pergunta":"...","resposta":"..."}}]

REGRAS DE FORMATACAO:
- Use aspas duplas para chaves e valores
- Dentro do conteudo, use aspas simples (nao duplas)
- Nao quebre linhas dentro de strings (use \\n se precisar)
- Nao adicione comentarios, apenas o JSON
- Garanta que o JSON termine completo com ]

CONTEUDO DA AULA:
{transcricao_bruta}"""

    resp = _call_with_retry(prompt, max_tokens=max_tokens)
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

    resp = _call_with_retry(prompt, max_tokens=6000)
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
{transcricao_bruta[:8000]}"""

    resp = _call_with_retry(prompt)
    return (resp.text or "").strip()
