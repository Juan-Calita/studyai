"""Pipeline com etapas paralelas e otimizacao para partes de sessao."""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.database import get_conn
from app.services import transcription, llm, pdf_generator, anki_export


def _update_status(conn, aula_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields.keys())
    values = list(fields.values()) + [aula_id]
    conn.execute(f"UPDATE aulas SET {sets} WHERE id=?", values)
    conn.commit()


def _sanitizar_flashcards(cards):
    """Aceita varios formatos de chaves e remove duplicatas."""
    resultado = []
    perguntas_vistas = set()
    if not isinstance(cards, list):
        return resultado
    for c in cards:
        if not isinstance(c, dict):
            continue
        pergunta = c.get('pergunta') or c.get('question') or c.get('front') or c.get('q')
        resposta = c.get('resposta') or c.get('answer') or c.get('back') or c.get('a')
        if not pergunta or not resposta:
            continue
        p_limpo = str(pergunta).strip()[:500]
        r_limpo = str(resposta).strip()[:1000]
        chave = p_limpo.lower()
        if chave in perguntas_vistas:
            continue
        perguntas_vistas.add(chave)
        resultado.append({'pergunta': p_limpo, 'resposta': r_limpo})
    return resultado


def processar_aula(aula_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM aulas WHERE id = ?", (aula_id,)).fetchone()
        if not row:
            return

        is_parte_sessao = bool(row["sessao_id"])

        # ====================================================
        # ETAPA 1: TRANSCRICAO (obrigatoria)
        # ====================================================
        print(f"[Pipeline] Aula {aula_id}: transcrevendo...")
        _update_status(conn, aula_id, status="transcrevendo")
        texto_bruto = transcription.transcrever(row["audio_path"])

        if not texto_bruto or len(texto_bruto.strip()) < 50:
            raise RuntimeError("Transcricao vazia. Audio pode estar corrompido.")

        _update_status(conn, aula_id, status="estruturando",
                       transcricao=texto_bruto,
                       resumo="Processando conteudo expandido...")

        # ====================================================
        # ETAPA 2: RESUMO (sempre necessario)
        # ====================================================
        print(f"[Pipeline] Aula {aula_id}: gerando resumo...")
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
            print(f"[Pipeline] Resumo falhou: {e}")
            resumo = "Nao foi possivel gerar resumo expandido."

        _update_status(conn, aula_id, titulo=titulo, resumo=resumo,
                       transcricao=transcricao_estruturada)

        # ====================================================
        # ETAPA 3: FLASHCARDS (sempre necessario)
        # Para partes de sessao: apenas flashcards, sem palacio/guia/pdf/anki
        # Para aulas normais: palacio + flashcards + guia em paralelo
        # ====================================================
        flashcards = []
        palacio = ""
        guia = ""

        if is_parte_sessao:
            # Partes de sessao: apenas flashcards (palacio/guia/pdf/anki serao feitos na compilacao)
            print(f"[Pipeline] Aula {aula_id}: parte de sessao - apenas flashcards...")
            _update_status(conn, aula_id, status="gerando_flashcards")
            try:
                cards_raw = llm.gerar_flashcards(texto_bruto)
                flashcards = _sanitizar_flashcards(cards_raw)
                print(f"[Pipeline] Flashcards: {len(flashcards)} cards")
            except Exception as e:
                print(f"[Pipeline] Flashcards falharam: {e}")

        else:
            # Aulas normais: palacio + flashcards + guia em paralelo
            print(f"[Pipeline] Aula {aula_id}: gerando palacio + flashcards + guia em paralelo...")
            _update_status(conn, aula_id, status="gerando_resumo")

            def _gerar_palacio():
                try:
                    result = llm.gerar_palacio_mental(texto_bruto, titulo)
                    print(f"[Pipeline] Palacio OK ({len(result)} chars)")
                    return result
                except Exception as e:
                    print(f"[Pipeline] Palacio falhou: {e}")
                    return ""

            def _gerar_flashcards_task():
                try:
                    cards_raw = llm.gerar_flashcards(texto_bruto)
                    result = _sanitizar_flashcards(cards_raw)
                    print(f"[Pipeline] Flashcards: {len(result)} cards")
                    return result
                except Exception as e:
                    print(f"[Pipeline] Flashcards falharam: {e}")
                    return []

            def _gerar_guia():
                try:
                    result = llm.gerar_guia_completo(texto_bruto)
                    print(f"[Pipeline] Guia OK ({len(result)} chars)")
                    return result
                except Exception as e:
                    print(f"[Pipeline] Guia falhou: {e}")
                    return ""

            with ThreadPoolExecutor(max_workers=3) as executor:
                fut_palacio = executor.submit(_gerar_palacio)
                fut_flashcards = executor.submit(_gerar_flashcards_task)
                fut_guia = executor.submit(_gerar_guia)
                palacio = fut_palacio.result()
                flashcards = fut_flashcards.result()
                guia = fut_guia.result()

        # Retry flashcards se vieram poucos
        if len(flashcards) < 5:
            print(f"[Pipeline] So {len(flashcards)} cards, fazendo retry final...")
            try:
                cards_extra = llm._gerar_flashcards_fallback(texto_bruto, 30)
                cards_extra_san = _sanitizar_flashcards(cards_extra)
                perguntas_existentes = {c['pergunta'].lower() for c in flashcards}
                for c in cards_extra_san:
                    if c['pergunta'].lower() not in perguntas_existentes:
                        flashcards.append(c)
                print(f"[Pipeline] Apos retry: {len(flashcards)} cards")
            except Exception as e:
                print(f"[Pipeline] Retry final falhou: {e}")

        if not flashcards:
            flashcards = [{
                "pergunta": f"Qual e o tema central de '{titulo}'?",
                "resposta": resumo[:300] if resumo else "Veja a transcricao da aula."
            }]

        for c in flashcards:
            conn.execute(
                "INSERT INTO flashcards (aula_id, pergunta, resposta) VALUES (?, ?, ?)",
                (aula_id, c["pergunta"], c["resposta"])
            )
        conn.commit()

        # ====================================================
        # ETAPA 4: PDF + ANKI (apenas aulas normais, em paralelo)
        # ====================================================
        pdf_path = None
        anki_path = None

        if not is_parte_sessao:
            _update_status(conn, aula_id, status="gerando_arquivos")
            estruturado_pdf = {
                'guia_de_estudos': guia,
                'resumo_expandido': resumo,
                'palacio_mental': palacio,
                'transcricao_estruturada': transcricao_estruturada,
            }

            print(f"[Pipeline] Aula {aula_id}: gerando PDF + Anki em paralelo...")

            def _gerar_pdf():
                try:
                    path = pdf_generator.gerar_pdf(
                        aula_id, titulo, texto_bruto, estruturado_pdf, flashcards
                    )
                    print(f"[Pipeline] PDF OK: {path}")
                    return path
                except Exception as e:
                    print(f"[Pipeline] PDF falhou: {e}")
                    return None

            def _gerar_anki():
                try:
                    path = anki_export.gerar_anki(aula_id, titulo, flashcards)
                    print(f"[Pipeline] Anki OK: {path}")
                    return path
                except Exception as e:
                    print(f"[Pipeline] Anki falhou: {e}")
                    return None

            with ThreadPoolExecutor(max_workers=2) as executor:
                fut_pdf = executor.submit(_gerar_pdf)
                fut_anki = executor.submit(_gerar_anki)
                pdf_path = fut_pdf.result()
                anki_path = fut_anki.result()

        # ====================================================
        # FINAL
        # ====================================================
        _update_status(conn, aula_id, status="pronto",
                       pdf_path=pdf_path, anki_path=anki_path)
        print(f"[Pipeline] Aula {aula_id}: CONCLUIDO! ({len(flashcards)} flashcards)")

        if row["sessao_id"]:
            _verificar_sessao(conn, row["sessao_id"])

    except Exception as e:
        import traceback
        print(f"[Pipeline] Aula {aula_id}: ERRO FATAL: {e}")
        print(traceback.format_exc())
        sessao_id_on_error = None
        try:
            row_err = conn.execute("SELECT sessao_id FROM aulas WHERE id=?", (aula_id,)).fetchone()
            if row_err:
                sessao_id_on_error = row_err["sessao_id"]
            conn.execute(
                "UPDATE aulas SET status='erro', erro=? WHERE id=?",
                (str(e)[:500], aula_id)
            )
            conn.commit()
        except Exception:
            pass
        if sessao_id_on_error:
            try:
                conn.execute(
                    "UPDATE sessoes SET status='erro', erro=? WHERE id=?",
                    (f"Parte {aula_id} falhou: {str(e)[:400]}", sessao_id_on_error)
                )
                conn.commit()
            except Exception:
                pass
    finally:
        conn.close()


def _verificar_sessao(conn, sessao_id: int):
    """Checa se todas as partes da sessao estao prontas e dispara compilacao."""
    sessao = conn.execute("SELECT * FROM sessoes WHERE id=?", (sessao_id,)).fetchone()
    if not sessao or sessao["status"] not in ("processando", "aguardando"):
        return

    total = sessao["total_partes"]
    prontas = conn.execute(
        "SELECT COUNT(*) FROM aulas WHERE sessao_id=? AND status='pronto'",
        (sessao_id,)
    ).fetchone()[0]

    print(f"[Pipeline] Sessao {sessao_id}: {prontas}/{total} partes prontas")

    if prontas >= total:
        print(f"[Pipeline] Sessao {sessao_id}: todas as partes prontas, compilando...")
        from app.services.compilar_sessao import compilar_sessao
        t = threading.Thread(target=compilar_sessao, args=(sessao_id,), daemon=True)
        t.start()
