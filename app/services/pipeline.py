from app.database import get_conn
from app.services import transcription, llm, pdf_generator, anki_export


def _update_status(conn, aula_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields.keys())
    values = list(fields.values()) + [aula_id]
    conn.execute(f"UPDATE aulas SET {sets} WHERE id=?", values)
    conn.commit()


def _sanitizar_flashcards(cards):
    """Aceita vários formatos: {pergunta/resposta}, {question/answer}, {front/back}, etc."""
    resultado = []
    if not isinstance(cards, list):
        return resultado
    for c in cards:
        if not isinstance(c, dict):
            continue
        # Tenta várias chaves possíveis
        pergunta = c.get('pergunta') or c.get('question') or c.get('front') or c.get('q')
        resposta = c.get('resposta') or c.get('answer') or c.get('back') or c.get('a')
        if pergunta and resposta:
            resultado.append({
                'pergunta': str(pergunta).strip(),
                'resposta': str(resposta).strip(),
            })
    return resultado


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

        # 2. Salva transcrição parcial
        _update_status(conn, aula_id, status="estruturando",
                       transcricao=texto_bruto,
                       resumo="Processando conteúdo expandido...")

        # 3. Chamada única unificada
        print(f"[Pipeline] Aula {aula_id}: processando tudo...")
        _update_status(conn, aula_id, status="gerando_conteudo")
        dados = llm.processar_tudo(texto_bruto)

        titulo = dados.get('titulo_sugerido') or row["titulo"]
        flashcards = _sanitizar_flashcards(dados.get('flashcards', []))

        # Fallback se vieram poucos flashcards
        if len(flashcards) < 5:
            print(f"[Pipeline] Poucos flashcards ({len(flashcards)}), tentando regenerar...")
            try:
                from app.services.llm import _call_with_retry, _parse_json
                resp = _call_with_retry(f"""Crie 20 flashcards da aula abaixo.
Regras: perguntas específicas, respostas de 1-3 frases.

Responda SÓ com JSON válido neste formato EXATO:
[{{"pergunta": "...", "resposta": "..."}}, {{"pergunta": "...", "resposta": "..."}}]

Aula:
{dados.get('transcricao_destrinchada', texto_bruto)[:8000]}""")
                novos = _sanitizar_flashcards(_parse_json(resp.text))
                if len(novos) > len(flashcards):
                    flashcards = novos
            except Exception as e:
                print(f"[Pipeline] Fallback de flashcards falhou: {e}")

        # Garante pelo menos 1 flashcard
        if not flashcards:
            flashcards = [{"pergunta": "Qual é o tema da aula?",
                          "resposta": titulo}]

        # 4. Salva dados parciais
        _update_status(conn, aula_id, status="gerando_arquivos",
                       titulo=titulo,
                       resumo=dados.get('resumo_expandido', ''),
                       transcricao=dados.get('transcricao_destrinchada', texto_bruto))

        for c in flashcards:
            conn.execute("INSERT INTO flashcards (aula_id, pergunta, resposta) VALUES (?, ?, ?)",
                         (aula_id, c["pergunta"], c["resposta"]))
        conn.commit()

        # 5. PDF
        estruturado_pdf = {
            'guia_de_estudos': dados.get('guia_de_estudos', ''),
            'resumo_expandido': dados.get('resumo_expandido', ''),
            'palacio_mental': dados.get('palacio_mental', ''),
            'transcricao_estruturada': dados.get('transcricao_destrinchada', texto_bruto),
        }

        print(f"[Pipeline] Aula {aula_id}: gerando PDF...")
        pdf_path = pdf_generator.gerar_pdf(aula_id, titulo, texto_bruto, estruturado_pdf, flashcards)

        # 6. Anki
        print(f"[Pipeline] Aula {aula_id}: gerando Anki...")
        anki_path = anki_export.gerar_anki(aula_id, titulo, flashcards)

        _update_status(conn, aula_id, status="pronto",
                       pdf_path=pdf_path, anki_path=anki_path)
        print(f"[Pipeline] Aula {aula_id}: ✅ concluído!")

    except Exception as e:
        import traceback
        print(f"[Pipeline] Aula {aula_id}: ❌ erro: {e}")
        print(traceback.format_exc())
        conn.execute("UPDATE aulas SET status='erro', erro=? WHERE id=?", (str(e), aula_id))
        conn.commit()
        raise
    finally:
        conn.close()
