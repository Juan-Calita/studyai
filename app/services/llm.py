"""LLM service: 4 funcoes isoladas. Falha em uma nao derruba as outras."""
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
    """Extrai JSON robustamente, com auto-repair de chaves nao fechadas."""
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


def _call_with_retry(prompt, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
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
# BLOCO 2: PALACIO MENTAL (com as 7 regras tecnicas)
# ============================================================

def gerar_palacio_mental(transcricao_bruta: str, titulo: str = "") -> str:
    """Gera palacio mental real e funcional, seguindo regras tecnicas rigorosas."""
    prompt = f"""Construa um PALACIO MENTAL real e funcional para o conteudo da aula abaixo. Voce DEVE seguir RIGOROSAMENTE as 7 regras tecnicas:

REGRA 1 - Bloque o conteudo antes de construir
Identifique conceitos principais, detalhes secundarios e ordem logica. Se for doenca: definicao -> causa -> fisiopatologia -> clinica -> diagnostico -> tratamento -> complicacoes. Se for classificacao: cada categoria separada. Se for processo: preserve a sequencia.

REGRA 2 - Escolha um lugar concreto e estavel
Use lugar especifico e visualizavel (uma casa, hospital, faculdade, igreja, academia). NAO use ambientes vagos ou genericos. Defina uma ROTA FIXA com pontos numerados. Cada ponto e um LOCUS.

REGRA 3 - Um locus, uma ideia principal
Nao sobrecarregue locus com muita informacao. Se a ideia tem muitos detalhes, use SUB-LOCI (ex: locus = sofa; sub-loci = almofada esquerda, almofada direita, encosto, braco).

REGRA 4 - Converta abstracoes em IMAGENS CONCRETAS E ATIVAS
Conceitos viram objetos/personagens/simbolos/cenas absurdas. Exemplos:
- Inflamacao -> fogo
- Hipertensao -> esfigmomanometro esmagando algo
- Anemia -> pessoa palida carregando hemacias vazias
- Obstrucao -> porta bloqueada
- Degeneracao -> algo apodrecendo
A imagem PRECISA AGIR no ambiente. NAO descreva cenas paradas. Use movimento, exagero, humor, estranheza, violencia simbolica, sons, cheiros. Quanto mais absurda, melhor (sem distorcer o conteudo real).

REGRA 5 - Ordem da rota = ordem do conteudo
Primeiro locus = primeiro evento. Para comparacoes, dois lados do ambiente. Para criterios, objetos numerados. Para excecoes, quebre o padrao da cena.

REGRA 6 - Cada locus precisa de uma PISTA curta
Frase de 2-5 palavras que ativa a lembranca. Ex: "pressao esmagando", "fogo inflamatorio", "porta bloqueada".

REGRA 7 - Tamanho ideal: 7 a 15 loci
NAO crie palacios gigantes. Maximo 15 loci.

==========================================
ESTRUTURA OBRIGATORIA DA SAIDA (markdown puro, ~800 palavras)
==========================================

# Palacio Mental: {titulo or "[tema da aula]"}

## Lugar escolhido
[Descreva em 2-3 frases o local concreto e por que e estavel/visualizavel]

## Rota fixa
[Liste os 7-15 loci em ordem numerada]

## Loci

### Locus 1: [Nome do local fisico] - [Conceito que guarda]
**Cena:** [Imagem ATIVA com movimento e absurdo. 3-5 frases.]
**Pista:** [frase curta de 2-5 palavras]
**Conteudo real:** [O que a cena representa de verdade na aula]

### Locus 2: ... [mesmo formato]
[Continue para todos os loci]

## Caminhada mental sugerida
[Texto narrativo de ~150 palavras percorrendo a rota do inicio ao fim como uma historia]

## Treinos
- **Ordem direta:** percorra do locus 1 ao ultimo
- **Ordem inversa:** comece do ultimo e volte ate o primeiro
- **Ordem aleatoria:** [3 perguntas tipo "o que esta no locus X?"]

==========================================
RESPOSTA
==========================================
Retorne APENAS o markdown puro do palacio (sem JSON, sem aspas envolvendo, sem comentarios).

Transcricao da aula:
{transcricao_bruta[:10000]}"""

    resp = _call_with_retry(prompt)
    return (resp.text or "").strip()


# ============================================================
# BLOCO 3: FLASHCARDS (25 cards focados em precisao)
# ============================================================

def gerar_flashcards(transcricao_bruta: str) -> list:
    """Gera 25 flashcards focados em valores numericos, criterios, doses e definicoes exatas."""
    prompt = f"""Crie 25 flashcards de estudo a partir da aula abaixo.

REGRAS:
- Perguntas claras e especificas (evite perguntas genericas tipo "o que e X?")
- Respostas concisas (1-3 frases, max 60 palavras)
- FOQUE em precisao: valores numericos, criterios formais, doses, contraindicacoes, definicoes exatas, classificacoes especificas
- Distribua os cards por todo o conteudo da aula
- Inclua pelo menos 5 cards sobre detalhes tecnicos finos (numeros, percentuais, criterios)

Responda APENAS com JSON valido neste formato exato:
[{{"pergunta":"...","resposta":"..."}},{{"pergunta":"...","resposta":"..."}}]

Aula:
{transcricao_bruta[:12000]}"""

    resp = _call_with_retry(prompt)
    return _parse_json(resp.text)


# ============================================================
# BLOCO 4: GUIA DE ESTUDOS + BIBLIOGRAFIA (juntos)
# ============================================================

def gerar_guia_completo(transcricao_bruta: str) -> str:
    """Gera guia programatico + bibliografia num unico markdown."""
    prompt = f"""Analise o tema da aula abaixo e crie um GUIA DE ESTUDOS COMPLETO em markdown, com DUAS PARTES bem separadas.

==========================================
PARTE 1 - CONTEUDO PROGRAMATICO COMPLETO
==========================================
Crie um conteudo programatico para aprender o tema da aula, do nivel iniciante ao avancado.
Organize em 4 a 6 modulos. Para cada modulo:

### Modulo X: [Nome do modulo] (Nivel: Iniciante / Intermediario / Avancado)

**Fundamentos essenciais (dominar primeiro):**
- Topico 1 (mais prioritario)
- Topico 2
- Topico 3

**Topicos complementares:**
- Topico 4
- Topico 5

==========================================
PARTE 2 - BIBLIOGRAFIA POR MODULO
==========================================
Para CADA modulo da Parte 1, indique de 1 a 5 livros ou artigos cientificos confiaveis.
Prefira materiais em portugues. Aceite ingles se for referencia fundamental.

### Bibliografia - Modulo X: [Nome]
1. **[Titulo do livro/artigo]** - [Autor(es)], [Ano]
   *Por que ler:* [explicacao em 2 linhas sobre relevancia para iniciantes]

2. **[Titulo]** - [Autor(es)], [Ano]
   *Por que ler:* [2 linhas]

==========================================
RESPOSTA
==========================================
Retorne APENAS o markdown puro (sem JSON, sem aspas envolvendo, sem comentarios).

Aula:
{transcricao_bruta[:8000]}"""

    resp = _call_with_retry(prompt)
    return (resp.text or "").strip()
