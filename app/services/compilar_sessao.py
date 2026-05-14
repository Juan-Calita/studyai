"""Compila todas as partes de uma sessao em um unico PDF e deck Anki."""
import traceback
from concurrent.futures import ThreadPoolExecutor
from app.database import get_conn
from app.services import llm, pdf_generator, anki_export


def _update_sessao(conn, sessao_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields.keys())
    values = list(fields.values()) + [sessao_id]
    conn.execute(f"UPDATE sessoes SET {sets} WHERE id=?", values)
    conn.commit()


def compilar_sessao(sessao_id: int):
    conn = get_conn()
    try:
        sessao = conn.execute("SELECT * FROM sessoes WHERE id=?", (sessao_id,)).fetchone()
        if not sessao:
            return

        print(f"[Compilar] Sessao {sessao_id}: iniciando compilacao...")
        _update_sessao(conn, sessao_id, status="compilando")

        partes = conn.execute(
            "SELECT * FROM aulas WHERE sessao_id=? ORDER BY numero_parte ASC",
            (sessao_id,)
        ).fetchall()

        if not partes:
            raise RuntimeError("Nenhuma parte encontrada para compilar.")

        transcricoes = []
        for p in partes:
            t = (p["transcricao"] or "").strip()
            if t:
                transcricoes.append(f"=== PARTE {p['numero_parte']} ===\n{t}")

        texto_completo = "\n\n".join(transcricoes)
        titulo = sessao["titulo"]

        print(f"[Compilar] Sessao {sessao_id}: texto unificado ({len(texto_completo)} chars)")
        _update_sessao(conn, sessao_id, status="gerando_resumo", transcricao=texto_completo)

        # ====================================================
        # ETAPA 1: RESUMO (sequencial - titulo necessario para proximas etapas)
        # ====================================================
        resumo = ""
        transcricao_estruturada = texto_completo
        try:
            resumo_data = llm.gerar_resumo(texto_completo)
            titulo = resumo_data.get("titulo_sugerido") or titulo
            resumo = resumo_data.get("resumo_expandido", "")
            transcricao_estruturada = resumo_data.get("transcricao_destrinchada", texto_completo)
            print(f"[Compilar] Resumo OK ({len(resumo)} chars)")
        except Exception as e:
            print(f"[Compilar] Resumo falhou: {e}")
            resumo = "Nao foi possivel gerar resumo expandido."

        _update_sessao(conn, sessao_id, resumo=resumo, transcricao=transcricao_estruturada)

        # ====================================================
        # ETAPA 2: PALACIO + FLASHCARDS EXTRAS + GUIA em paralelo
        # ====================================================
        from app.services.pipeline import _sanitizar_flashcards

        flashcards_existentes = conn.execute(
            "SELECT pergunta, resposta FROM flashcards WHERE aula_id IN "
            "(SELECT id FROM aulas WHERE sessao_id=?)",
            (sessao_id,)
        ).fetchall()
        flashcards = [{"pergunta": f["pergunta"], "resposta": f["resposta"]} for f in flashcards_existentes]

        _update_sessao(conn, sessao_id, status="gerando_resumo")
        titulo_final = titulo

        def _gerar_palacio():
            try:
                result = llm.gerar_palacio_mental(texto_completo, titulo_final)
                print(f"[Compilar] Palacio OK ({len(result)} chars)")
                return result
            except Exception as e:
                print(f"[Compilar] Palacio falhou: {e}")
                return ""

        def _gerar_flashcards_extras():
            try:
                cards_extra = llm.gerar_flashcards(texto_completo)
                result = _sanitizar_flashcards(cards_extra)
                print(f"[Compilar] Flashcards extras: {len(result)} cards")
                return result
            except Exception as e:
                print(f"[Compilar] Flashcards extras falharam: {e}")
                return []

        def _gerar_guia():
            try:
                result = llm.gerar_guia_completo(texto_completo)
                print(f"[Compilar] Guia OK ({len(result)} chars)")
                return result
            except Exception as e:
                print(f"[Compilar] Guia falhou: {e}")
                return ""

        print(f"[Compilar] Sessao {sessao_id}: gerando palacio + flashcards + guia em paralelo...")
        with ThreadPoolExecutor(max_workers=3) as executor:
            fut_palacio = executor.submit(_gerar_palacio)
            fut_extras = executor.submit(_gerar_flashcards_extras)
            fut_guia = executor.submit(_gerar_guia)
            palacio = fut_palacio.result()
            cards_extras = fut_extras.result()
            guia = fut_guia.result()

        # Merge flashcards sem duplicatas
        perguntas_existentes = {c["pergunta"].lower() for c in flashcards}
        for c in cards_extras:
            if c["pergunta"].lower() not in perguntas_existentes:
                flashcards.append(c)

        print(f"[Compilar] Total flashcards: {len(flashcards)}")

        if not flashcards:
            flashcards = [{"pergunta": f"Qual o tema central de '{titulo}'?",
                           "resposta": resumo[:300] if resumo else "Veja a transcricao."}]

        for c in flashcards:
            conn.execute(
                "INSERT INTO flashcards (sessao_id, aula_id, pergunta, resposta) VALUES (?,NULL,?,?)",
                (sessao_id, c["pergunta"], c["resposta"])
            )
        conn.commit()

        # ====================================================
        # ETAPA 3: PDF + ANKI em paralelo
        # ====================================================
        _update_sessao(conn, sessao_id, status="gerando_arquivos")
        estruturado = {
            "guia_de_estudos": guia,
            "resumo_expandido": resumo,
            "palacio_mental": palacio,
            "transcricao_estruturada": transcricao_estruturada,
        }

        print(f"[Compilar] Sessao {sessao_id}: gerando PDF + Anki em paralelo...")

        def _gerar_pdf():
            try:
                path = pdf_generator.gerar_pdf(
                    -sessao_id, titulo, texto_completo, estruturado, flashcards
                )
                print(f"[Compilar] PDF OK: {path}")
                return path
            except Exception as e:
                print(f"[Compilar] PDF falhou: {e}")
                return None

        def _gerar_anki():
            try:
                path = anki_export.gerar_anki(-sessao_id, titulo, flashcards)
                print(f"[Compilar] Anki OK: {path}")
                return path
            except Exception as e:
                print(f"[Compilar] Anki falhou: {e}")
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_pdf = executor.submit(_gerar_pdf)
            fut_anki = executor.submit(_gerar_anki)
            pdf_path = fut_pdf.result()
            anki_path = fut_anki.result()

        _update_sessao(conn, sessao_id, status="pronto",
                       pdf_path=pdf_path, anki_path=anki_path)
        print(f"[Compilar] Sessao {sessao_id}: CONCLUIDA! ({len(flashcards)} flashcards)")

    except Exception as e:
        print(f"[Compilar] Sessao {sessao_id}: ERRO FATAL: {e}")
        print(traceback.format_exc())
        try:
            conn.execute(
                "UPDATE sessoes SET status='erro', erro=? WHERE id=?",
                (str(e)[:500], sessao_id)
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
