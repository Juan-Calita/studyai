import json
from app.database import get_conn
from app.services import transcription, llm, embeddings, pdf_generator, anki_export


def processar_aula(aula_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM aulas WHERE id = ?", (aula_id,)).fetchone()
        if not row:
            return

        # 1. Transcrição
        print(f"[Pipeline] Aula {aula_id}: transcrevendo...")
        texto_bruto = transcription.transcrever(row["audio_path"])

        # 2. Estruturação com Gemini
        print(f"[Pipeline] Aula {aula_id}: estruturando com Gemini...")
        estruturado = llm.estruturar_transcricao(texto_bruto)
        titulo = estruturado.get("titulo_sugerido") or row["titulo"]
        resumo = estruturado["resumo"]
        transcricao = estruturado["transcricao_estruturada"]

        # 3. Embedding
        print(f"[Pipeline] Aula {aula_id}: gerando embedding...")
        texto_emb = f"{resumo}\n\n{transcricao[:3000]}"
        emb = embeddings.embed_one(texto_emb)

        # 4. Flashcards
        print(f"[Pipeline] Aula {aula_id}: gerando flashcards...")
        cards = llm.gerar_flashcards(transcricao, n=15)
        for c in cards:
            conn.execute(
                "INSERT INTO flashcards (aula_id, pergunta, resposta) VALUES (?, ?, ?)",
                (aula_id, c["pergunta"], c["resposta"]),
            )

        # 5. PDF
        print(f"[Pipeline] Aula {aula_id}: gerando PDF...")
        pdf_path = pdf_generator.gerar_pdf(aula_id, titulo, resumo, transcricao, cards)

        # 6. Anki
        print(f"[Pipeline] Aula {aula_id}: gerando Anki...")
        anki_path = anki_export.gerar_anki(aula_id, titulo, cards)

        # Atualiza banco
        conn.execute("""
            UPDATE aulas SET titulo=?, resumo=?, transcricao=?, embedding=?,
                            pdf_path=?, anki_path=?, status='pronto'
            WHERE id=?
        """, (titulo, resumo, transcricao, json.dumps(emb), pdf_path, anki_path, aula_id))
        conn.commit()
        print(f"[Pipeline] Aula {aula_id}: ✅ concluído!")

    except Exception as e:
        print(f"[Pipeline] Aula {aula_id}: ❌ erro: {e}")
        conn.execute("UPDATE aulas SET status='erro', erro=? WHERE id=?", (str(e), aula_id))
        conn.commit()
        raise
    finally:
        conn.close()
