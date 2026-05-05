from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from app.database import get_conn
from app.services import transcription, llm, pdf_generator, anki_export


def _update_status(conn, aula_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields.keys())
    values = list(fields.values()) + [aula_id]
    conn.execute(f"UPDATE aulas SET {sets} WHERE id=?", values)
    conn.commit()


def _sanitizar_flashcards(cards):
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

        # === ETAPA 1: Transcrição ===
        print(f"[Pipeline] Aula {aula_id}: transcrevendo...")
        _update_status(conn, aula_id, status="transcrevendo")
        texto_bruto = transcription.transcrever(row["audio_path"])
        if not texto_bruto or len(texto_bruto.strip()) < 50:
            raise RuntimeError("Transcrição vazia ou muito curta. O áudio pode estar corrompido.")

        # Salva transcrição bruta imediatamente (resultado parcial pro usuário)
        _update_status(conn, aula_id, status="estruturando",
                       transcricao=texto_bruto,
                       resumo="Estruturando conteúdo expandido...")

        # === ETAPA 2: Estrutura + Flashcards EM PARALELO ===
        print(f"[Pipeline] Aula {aula_id}: rodando estrutura + flashcards em paralelo...")
        _update_status(conn, aula_id, status="gerando_conteudo")

        estrutura = None
        flashcards_raw = []

        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_estrutura = ex.submit(llm.gerar_estrutura, texto_bruto)
            fut_flashcards = ex.submit(llm.gerar_flashcards, texto_bruto)
            try:
                estrutura = fut_estrutura.result(timeout=240)
            except Exception as e:
                print(f"[Pipeline] Estrutura falhou: {e}")
            try:
                flashcards_raw = fut_flashcards.result(timeout=180)
            except Exception as e:
                print(f"[Pipeline] Flashcards falharam: {e}")

        # Fallback: se estrutura falhou, usa transcrição bruta
        if not estrutura:
            estrutura = {
                'titulo_sugerido': row["titulo"],
                'resumo_expandido': 'Não foi possível gerar resumo expandido. Use a transcrição abaixo como base.',
                'transcricao_destrinchada': texto_bruto,
            }

        titulo = estrutura.get('titulo_sugerido') or row["titulo"]
        resumo = estrutura.get('resumo_expandido', '')
        transcricao_estruturada = estrutura.get('transcricao_destrinchada', texto_bruto)
        flashcards = _sanitizar_flashcards(flashcards_raw)

        # Garante pelo menos um flashcard mínimo
        if not flashcards:
            flashcards = [{
                "pergunta": f"Qual é o tema central de '{titulo}'?",
                "resposta": resumo[:300] if resumo else "Veja a transcrição da aula."
            }]

        # Salva conteúdo principal
        _update_status(conn, aula_id, status="gerando_arquivos",
                       titulo=titulo, resumo=resumo, transcricao=transcricao_estruturada)

        for c in flashcards:
            conn.execute("INSERT INTO flashcards (aula_id, pergunta, resposta) VALUES (?, ?, ?)",
                         (aula_id, c["pergunta"], c["resposta"]))
        conn.commit()

        # === ETAPA 3 (opcional): Extras (guia + palácio) ===
        guia = ''
        palacio = ''
        try:
            print(f"[Pipeline] Aula {aula_id}: gerando extras...")
            extras = llm.gerar_extras(texto_bruto)
            guia = extras.get('guia_de_estudos', '')
            palacio = extras.get('palacio_mental', '')
        except Exception as e:
            print(f"[Pipeline] Extras falharam (sem problemas, prossegue): {e}")

        # === ETAPA 4: PDF ===
        estruturado_pdf = {
            'guia_de_estudos': guia,
            'resumo_expandido': resumo,
            'palacio_mental': palacio,
            'transcricao_estruturada': transcricao_estruturada,
        }

        print(f"[Pipeline] Aula {aula_id}: gerando PDF...")
        try:
            pdf_path = pdf_generator.gerar_pdf(aula_id, titulo, texto_bruto, estruturado_pdf, flashcards)
        except Exception as e:
            print(f"[Pipeline] PDF falhou: {e}")
            pdf_path = None

        # === ETAPA 5: Anki ===
        print(f"[Pipeline] Aula {aula_id}: gerando Anki...")
        try:
            anki_path = anki_export.gerar_anki(aula_id, titulo, flashcards)
        except Exception as e:
            print(f"[Pipeline] Anki falhou: {e}")
            anki_path = None

        _update_status(conn, aula_id, status="pronto",
                       pdf_path=pdf_path, anki_path=anki_path)
        print(f"[Pipeline] Aula {aula_id}: ✅ concluído!")

    except Exception as e:
        import traceback
        print(f"[Pipeline] Aula {aula_id}: ❌ erro: {e}")
        print(traceback.format_exc())
        try:
            conn.execute("UPDATE aulas SET status='erro', erro=? WHERE id=?", (str(e)[:500], aula_id))
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
