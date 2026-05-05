"""Pipeline simples e sequencial: transcricao -> processar_tudo -> PDF + Anki."""
from app.database import get_conn
from app.services import transcription, llm, pdf_generator, anki_export


def _update_status(conn, aula_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields.keys())
    values = list(fields.values()) + [aula_id]
    conn.execute(f"UPDATE aulas SET {sets} WHERE id=?", values)
    conn.commit()


def _sanitizar_flashcards(cards):
    """Aceita varios formatos de chaves: pergunta/question/front, resposta/answer/back."""
    resultado = []
    if not isinstance(cards, list):
        return resultado
    for c in cards:
        if not isinstance(c, dict):
            continue
        pergunta = c.get('pergunta') or c.get('question') or c.get('front') or c.get('q')
        resposta = c.get('resposta') or c.get('answer') or c.get('back') or c.get('a')
        if pergunta and resposta:
            resultado.append({
                'pergunta': str(pergunta).strip()[:500],
                'resposta': str(resposta).strip()[:1000],
            })
    return resultado


def processar_aula(aula_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM aulas WHERE id = ?", (aula_id,)).fetchone()
        if not row:
            return

        # === ETAPA 1: Transcricao ===
        print(f"[Pipeline] Aula {aula_id}: transcrevendo...")
        _update_status(conn, aula_id, status="transcrevendo")
        texto_bruto = transcription.transcrever(row["audio_path"])

        if not texto_bruto or len(texto_bruto.strip()) < 50:
            raise RuntimeError("Transcricao vazia ou muito curta. O audio pode estar corrompido.")

        # Salva transcricao bruta imediatamente
        _update_status(conn, aula_id, status="estruturando",
                       transcricao=texto_bruto,
                       resumo="Processando conteudo expandido...")

        # === ETAPA 2: Chamada UNICA que gera tudo ===
        print(f"[Pipeline] Aula {aula_id}: gerando conteudo (1 chamada unica)...")
        _update_status(conn, aula_id, status="gerando_conteudo")

        try:
            dados = llm.processar_tudo(texto_bruto)
        except Exception as e:
            print(f"[Pipeline] LLM falhou: {e}")
            # Fallback minimo: usa apenas transcricao bruta
            dados = {
                'titulo_sugerido': row["titulo"],
                'resumo_expandido': 'Nao foi possivel gerar resumo. Use a transcricao abaixo como base.',
                'guia_de_estudos': '',
                'palacio_mental': '',
                'transcricao_destrinchada': texto_bruto,
                'flashcards': [],
            }

        titulo = dados.get('titulo_sugerido') or row["titulo"]
        resumo = dados.get('resumo_expandido', '')
        transcricao_estruturada = dados.get('transcricao_destrinchada', texto_bruto)
        flashcards = _sanitizar_flashcards(dados.get('flashcards', []))

        # Garante pelo menos um flashcard
        if not flashcards:
            flashcards = [{
                "pergunta": f"Qual e o tema central de '{titulo}'?",
                "resposta": resumo[:300] if resumo else "Veja a transcricao da aula."
            }]

        # Salva conteudo principal
        _update_status(conn, aula_id, status="gerando_arquivos",
                       titulo=titulo, resumo=resumo,
                       transcricao=transcricao_estruturada)

        for c in flashcards:
            conn.execute(
                "INSERT INTO flashcards (aula_id, pergunta, resposta) VALUES (?, ?, ?)",
                (aula_id, c["pergunta"], c["resposta"])
            )
        conn.commit()

        # === ETAPA 3: PDF ===
        estruturado_pdf = {
            'guia_de_estudos': dados.get('guia_de_estudos', ''),
            'resumo_expandido': resumo,
            'palacio_mental': dados.get('palacio_mental', ''),
            'transcricao_estruturada': transcricao_estruturada,
        }

        print(f"[Pipeline] Aula {aula_id}: gerando PDF...")
        try:
            pdf_path = pdf_generator.gerar_pdf(
                aula_id, titulo, texto_bruto, estruturado_pdf, flashcards
            )
        except Exception as e:
            print(f"[Pipeline] PDF falhou: {e}")
            pdf_path = None

        # === ETAPA 4: Anki ===
        print(f"[Pipeline] Aula {aula_id}: gerando Anki...")
        try:
            anki_path = anki_export.gerar_anki(aula_id, titulo, flashcards)
        except Exception as e:
            print(f"[Pipeline] Anki falhou: {e}")
            anki_path = None

        _update_status(conn, aula_id, status="pronto",
                       pdf_path=pdf_path, anki_path=anki_path)
        print(f"[Pipeline] Aula {aula_id}: concluido!")

    except Exception as e:
        import traceback
        print(f"[Pipeline] Aula {aula_id}: ERRO: {e}")
        print(traceback.format_exc())
        try:
            conn.execute(
                "UPDATE aulas SET status='erro', erro=? WHERE id=?",
                (str(e)[:500], aula_id)
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
