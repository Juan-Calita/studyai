"""Compila todas as partes de uma sessão em um único PDF e deck Anki."""
import traceback
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

        # Busca todas as partes ordenadas pelo numero_parte
        partes = conn.execute(
            "SELECT * FROM aulas WHERE sessao_id=? ORDER BY numero_parte ASC",
            (sessao_id,)
        ).fetchall()

        if not partes:
            raise RuntimeError("Nenhuma parte encontrada para compilar.")

        # Junta transcricoes em ordem
        transcricoes = []
        for p in partes:
            t = (p["transcricao"] or "").strip()
            if t:
                transcricoes.append(f"=== PARTE {p['numero_parte']} ===\n{t}")

        texto_completo = "\n\n".join(transcricoes)
        titulo = sessao["titulo"]

        print(f"[Compilar] Sessao {sessao_id}: texto unificado ({len(texto_completo)} chars)")
        _update_sessao(conn, sessao_id, status="gerando_resumo", transcricao=texto_completo)

        # Gera resumo unificado
        resumo = ""
        transcricao_estruturada = texto_completo
        try:
            resumo_data = llm.gerar_resumo(texto_completo)
            titulo_sugerido = resumo_data.get("titulo_sugerido") or titulo
            resumo = resumo_data.get("resumo_expandido", "")
            transcricao_estruturada = resumo_data.get("transcricao_destrinchada", texto_completo)
            print(f"[Compilar] Resumo OK ({len(resumo)} chars)")
        except Exception as e:
            print(f"[Compilar] Resumo falhou: {e}")
            resumo = "Não foi possível gerar resumo expandido."

        _update_sessao(conn, sessao_id, resumo=resumo, transcricao=transcricao_estruturada)

        # Palácio mental
        _update_sessao(conn, sessao_id, status="gerando_palacio")
        palacio = ""
        try:
            palacio = llm.gerar_palacio_mental(texto_completo, titulo)
            print(f"[Compilar] Palacio OK ({len(palacio)} chars)")
        except Exception as e:
            print(f"[Compilar] Palacio falhou: {e}")

        # Flashcards: coleta de todas as partes + gera novos do texto completo
        _update_sessao(conn, sessao_id, status="gerando_flashcards")

        from app.services.pipeline import _sanitizar_flashcards
        flashcards_existentes = conn.execute(
            "SELECT pergunta, resposta FROM flashcards WHERE aula_id IN "
            "(SELECT id FROM aulas WHERE sessao_id=?)",
            (sessao_id,)
        ).fetchall()
        flashcards = [{"pergunta": f["pergunta"], "resposta": f["resposta"]} for f in flashcards_existentes]

        # Gera flashcards adicionais do texto completo
        try:
            cards_extra = llm.gerar_flashcards(texto_completo)
            cards_san = _sanitizar_flashcards(cards_extra)
            perguntas_existentes = {c["pergunta"].lower() for c in flashcards}
            for c in cards_san:
                if c["pergunta"].lower() not in perguntas_existentes:
                    flashcards.append(c)
            print(f"[Compilar] Total flashcards: {len(flashcards)}")
        except Exception as e:
            print(f"[Compilar] Flashcards extras falharam: {e}")

        if not flashcards:
            flashcards = [{"pergunta": f"Qual o tema central de '{titulo}'?",
                           "resposta": resumo[:300] if resumo else "Veja a transcrição."}]

        # Salva flashcards da sessão
        for c in flashcards:
            conn.execute(
                "INSERT INTO flashcards (sessao_id, aula_id, pergunta, resposta) VALUES (?,NULL,?,?)",
                (sessao_id, c["pergunta"], c["resposta"])
            )
        conn.commit()

        # Guia completo
        _update_sessao(conn, sessao_id, status="gerando_guia")
        guia = ""
        try:
            guia = llm.gerar_guia_completo(texto_completo)
            print(f"[Compilar] Guia OK ({len(guia)} chars)")
        except Exception as e:
            print(f"[Compilar] Guia falhou: {e}")

        # PDF
        _update_sessao(conn, sessao_id, status="gerando_arquivos")
        estruturado = {
            "guia_de_estudos": guia,
            "resumo_expandido": resumo,
            "palacio_mental": palacio,
            "transcricao_estruturada": transcricao_estruturada,
        }
        pdf_path = None
        try:
            # Usa id negativo para sessão (evita colisão com aulas)
            pdf_path = pdf_generator.gerar_pdf(
                -sessao_id, titulo, texto_completo, estruturado, flashcards
            )
            print(f"[Compilar] PDF OK: {pdf_path}")
        except Exception as e:
            print(f"[Compilar] PDF falhou: {e}")

        anki_path = None
        try:
            anki_path = anki_export.gerar_anki(-sessao_id, titulo, flashcards)
            print(f"[Compilar] Anki OK: {anki_path}")
        except Exception as e:
            print(f"[Compilar] Anki falhou: {e}")

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
