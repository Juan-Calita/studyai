"""Pipeline sequencial com falha isolada por bloco.
Se um bloco falhar, os outros continuam e o usuario sempre recebe algo."""
from app.database import get_conn
from app.services import transcription, llm, pdf_generator, anki_export


def _update_status(conn, aula_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields.keys())
    values = list(fields.values()) + [aula_id]
    conn.execute(f"UPDATE aulas SET {sets} WHERE id=?", values)
    conn.commit()


def _sanitizar_flashcards(cards):
    """Aceita varios formatos de chaves."""
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

        # ====================================================
        # ETAPA 1: TRANSCRICAO (obrigatoria - se falhar aborta)
        # ====================================================
        print(f"[Pipeline] Aula {aula_id}: transcrevendo...")
        _update_status(conn, aula_id, status="transcrevendo")
        texto_bruto = transcription.transcrever(row["audio_path"])

        if not texto_bruto or len(texto_bruto.strip()) < 50:
            raise RuntimeError("Transcricao vazia ou muito curta. Audio pode estar corrompido.")

        # Salva transcricao bruta imediatamente
        _update_status(conn, aula_id, status="estruturando",
                       transcricao=texto_bruto,
                       resumo="Processando conteudo expandido...")

        # ====================================================
        # ETAPA 2: RESUMO + TITULO + TRANSCRICAO ESTRUTURADA
        # ====================================================
        print(f"[Pipeline] Aula {aula_id}: bloco 1/4 - resumo...")
        _update_status(conn, aula_id, status="gerando_resumo")
        titulo = row["titulo"]
        resumo = ""
        transcricao_estruturada = texto_bruto

        try:
            resumo_data = llm.gerar_resumo(texto_bruto)
            titulo = resumo_data.get('titulo_sugerido') or row["titulo"]
            resumo = resumo_data.get('resumo_expandido', '')
            transcricao_estruturada = resumo_data.get('transcricao_destrinchada', texto_bruto)
            print(f"[Pipeline] Resumo OK ({len(resumo)} chars)")
        except Exception as e:
            print(f"[Pipeline] Resumo falhou (segue sem): {e}")
            resumo = "Nao foi possivel gerar resumo expandido. Use a transcricao abaixo."

        _update_status(conn, aula_id, titulo=titulo, resumo=resumo,
                       transcricao=transcricao_estruturada)

        # ====================================================
        # ETAPA 3: PALACIO MENTAL
        # ====================================================
        print(f"[Pipeline] Aula {aula_id}: bloco 2/4 - palacio mental...")
        _update_status(conn, aula_id, status="gerando_palacio")
        palacio = ""
        try:
            palacio = llm.gerar_palacio_mental(texto_bruto, titulo)
            print(f"[Pipeline] Palacio OK ({len(palacio)} chars)")
        except Exception as e:
            print(f"[Pipeline] Palacio falhou (segue sem): {e}")

        # ====================================================
        # ETAPA 4: FLASHCARDS
        # ====================================================
        print(f"[Pipeline] Aula {aula_id}: bloco 3/4 - flashcards...")
        _update_status(conn, aula_id, status="gerando_flashcards")
        flashcards = []
        try:
            cards_raw = llm.gerar_flashcards(texto_bruto)
            flashcards = _sanitizar_flashcards(cards_raw)
            print(f"[Pipeline] Flashcards OK ({len(flashcards)} cards)")
        except Exception as e:
            print(f"[Pipeline] Flashcards falharam (segue sem): {e}")

        # Garante pelo menos um flashcard pra UI nao quebrar
        if not flashcards:
            flashcards = [{
                "pergunta": f"Qual e o tema central de '{titulo}'?",
                "resposta": resumo[:300] if resumo else "Veja a transcricao da aula."
            }]

        # Salva flashcards no banco
        for c in flashcards:
            conn.execute(
                "INSERT INTO flashcards (aula_id, pergunta, resposta) VALUES (?, ?, ?)",
                (aula_id, c["pergunta"], c["resposta"])
            )
        conn.commit()

        # ====================================================
        # ETAPA 5: GUIA DE ESTUDOS + BIBLIOGRAFIA
        # ====================================================
        print(f"[Pipeline] Aula {aula_id}: bloco 4/4 - guia de estudos...")
        _update_status(conn, aula_id, status="gerando_guia")
        guia = ""
        try:
            guia = llm.gerar_guia_completo(texto_bruto)
            print(f"[Pipeline] Guia OK ({len(guia)} chars)")
        except Exception as e:
            print(f"[Pipeline] Guia falhou (segue sem): {e}")

        # ====================================================
        # ETAPA 6: PDF (consolida tudo que deu certo)
        # ====================================================
        _update_status(conn, aula_id, status="gerando_arquivos")
        estruturado_pdf = {
            'guia_de_estudos': guia,
            'resumo_expandido': resumo,
            'palacio_mental': palacio,
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

        # ====================================================
        # ETAPA 7: ANKI
        # ====================================================
        print(f"[Pipeline] Aula {aula_id}: gerando Anki...")
        try:
            anki_path = anki_export.gerar_anki(aula_id, titulo, flashcards)
        except Exception as e:
            print(f"[Pipeline] Anki falhou: {e}")
            anki_path = None

        # ====================================================
        # FINAL
        # ====================================================
        _update_status(conn, aula_id, status="pronto",
                       pdf_path=pdf_path, anki_path=anki_path)
        print(f"[Pipeline] Aula {aula_id}: CONCLUIDO!")

    except Exception as e:
        import traceback
        print(f"[Pipeline] Aula {aula_id}: ERRO FATAL: {e}")
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
