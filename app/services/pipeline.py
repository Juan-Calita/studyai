from app.database import get_conn
from app.services import transcription, llm, pdf_generator, anki_export


def processar_aula(aula_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM aulas WHERE id = ?", (aula_id,)).fetchone()
        if not row:
            return

        # 1. Transcrição
        print(f"[Pipeline] Aula {aula_id}: transcrevendo...")
        texto_bruto = transcription.transcrever(row["audio_path"])

        # 2. Estruturação expandida
        print(f"[Pipeline] Aula {aula_id}: estruturando (resumo + guia + palácio)...")
        estruturado = llm.estruturar_transcricao(texto_bruto)

        # 3. Deep dive (ênfase + 30+ flashcards)
        print(f"[Pipeline] Aula {aula_id}: deep dive...")
        try:
            dados_profundos = llm.deep_dive(texto_bruto)
            titulo = dados_profundos.get('titulo_detalhado') or estruturado.get('titulo_sugerido') or row["titulo"]
            estruturado['transcricao_estruturada'] = dados_profundos.get('transcricao_destrinchada', estruturado.get('transcricao_estruturada', ''))
            flashcards = dados_profundos.get('flashcards_extensivos', [])
        except Exception as e:
            print(f"[Pipeline] Deep dive falhou, usando fallback: {e}")
            titulo = estruturado.get('titulo_sugerido') or row["titulo"]
            flashcards = []

        # Fallback se não tiver flashcards
        if not flashcards:
            print(f"[Pipeline] Aula {aula_id}: gerando flashcards simples...")
            from app.services.llm import _call_with_retry, _parse_json
            resp = _call_with_retry(f"""Crie 15 flashcards da aula abaixo.
Responda SÓ com JSON: [{{"pergunta":"...","resposta":"..."}}]

Aula:
{estruturado.get('transcricao_estruturada', texto_bruto)}""")
            flashcards = _parse_json(resp.text)

        # Salvar flashcards no DB
        for c in flashcards:
            conn.execute("INSERT INTO flashcards (aula_id, pergunta, resposta) VALUES (?, ?, ?)",
                         (aula_id, c["pergunta"], c["resposta"]))

        # 4. PDF completo
        print(f"[Pipeline] Aula {aula_id}: gerando PDF...")
        pdf_path = pdf_generator.gerar_pdf(aula_id, titulo, texto_bruto, estruturado, flashcards)

        # 5. Anki
        print(f"[Pipeline] Aula {aula_id}: gerando Anki...")
        anki_path = anki_export.gerar_anki(aula_id, titulo, flashcards)

        conn.execute("""
            UPDATE aulas SET titulo=?, resumo=?, transcricao=?, pdf_path=?, anki_path=?, status='pronto'
            WHERE id=?
        """, (titulo, estruturado.get('resumo_expandido', ''),
              estruturado.get('transcricao_estruturada', texto_bruto),
              pdf_path, anki_path, aula_id))
        conn.commit()
        print(f"[Pipeline] Aula {aula_id}: ✅ concluído!")

    except Exception as e:
        print(f"[Pipeline] Aula {aula_id}: ❌ erro: {e}")
        conn.execute("UPDATE aulas SET status='erro', erro=? WHERE id=?", (str(e), aula_id))
        conn.commit()
        raise
    finally:
        conn.close()
