from app.database import get_conn
from app.services import transcription, llm, pdf_generator, anki_export


def _update_status(conn, aula_id, **fields):
    """Atualiza campos da aula e commita — para dar feedback parcial."""
    sets = ", ".join(f"{k}=?" for k in fields.keys())
    values = list(fields.values()) + [aula_id]
    conn.execute(f"UPDATE aulas SET {sets} WHERE id=?", values)
    conn.commit()


def processar_aula(aula_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM aulas WHERE id = ?", (aula_id,)).fetchone()
        if not row:
            return

        # 1. Transcrição
        print(f"[Pipeline] Aula {aula_id}: transcrevendo...")
        _update_status(conn, aula_id, status="transcrevendo")
        texto_bruto = transcription.transcrever(row["audio_path"])

        # 2. Salva transcrição bruta imediatamente (resultado parcial)
        _update_status(conn, aula_id, status="estruturando",
                       transcricao=texto_bruto,
                       resumo="Processando conteúdo expandido... (você já pode ler a transcrição)")

        # 3. Uma única chamada que gera tudo
        print(f"[Pipeline] Aula {aula_id}: processando tudo em uma chamada...")
        _update_status(conn, aula_id, status="gerando_conteudo")
        try:
            dados = llm.processar_tudo(texto_bruto)
        except Exception as e:
            print(f"[Pipeline] processar_tudo falhou: {e}")
            raise

        titulo = dados.get('titulo_sugerido') or row["titulo"]
        flashcards = dados.get('flashcards', [])

        # 4. Salva resumo e flashcards imediatamente (parcial)
        _update_status(conn, aula_id, status="gerando_arquivos",
                       titulo=titulo,
                       resumo=dados.get('resumo_expandido', ''),
                       transcricao=dados.get('transcricao_destrinchada', texto_bruto))

        for c in flashcards:
            conn.execute("INSERT INTO flashcards (aula_id, pergunta, resposta) VALUES (?, ?, ?)",
                         (aula_id, c["pergunta"], c["resposta"]))
        conn.commit()

        # 5. Adaptar formato pro PDF
        estruturado_pdf = {
            'guia_de_estudos': dados.get('guia_de_estudos', ''),
            'resumo_expandido': dados.get('resumo_expandido', ''),
            'palacio_mental': dados.get('palacio_mental', ''),
            'transcricao_estruturada': dados.get('transcricao_destrinchada', texto_bruto),
        }

        # 6. PDF
        print(f"[Pipeline] Aula {aula_id}: gerando PDF...")
        pdf_path = pdf_generator.gerar_pdf(aula_id, titulo, texto_bruto, estruturado_pdf, flashcards)

        # 7. Anki
        print(f"[Pipeline] Aula {aula_id}: gerando Anki...")
        anki_path = anki_export.gerar_anki(aula_id, titulo, flashcards)

        _update_status(conn, aula_id, status="pronto",
                       pdf_path=pdf_path, anki_path=anki_path)
        print(f"[Pipeline] Aula {aula_id}: ✅ concluído!")

    except Exception as e:
        print(f"[Pipeline] Aula {aula_id}: ❌ erro: {e}")
        conn.execute("UPDATE aulas SET status='erro', erro=? WHERE id=?", (str(e), aula_id))
        conn.commit()
        raise
    finally:
        conn.close()
