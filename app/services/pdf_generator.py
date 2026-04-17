from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.platypus import SimpleDocTemplate, Paragraph, PageBreak
from app.config import settings


def escape(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def gerar_pdf(aula_id: int, titulo: str, resumo: str, conteudo_md: str, flashcards: list[dict]) -> str:
    pdf_path = str(settings.pdf_dir / f"aula_{aula_id}.pdf")
    doc = SimpleDocTemplate(pdf_path, pagesize=A4, topMargin=2 * cm, bottomMargin=2 * cm)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Titulo", fontSize=20, textColor=HexColor("#1a365d"),
                              spaceAfter=14, fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name="H2", fontSize=14, textColor=HexColor("#2c5282"),
                              spaceAfter=10, spaceBefore=14, fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name="Corpo", fontSize=11, leading=16, spaceAfter=8))
    styles.add(ParagraphStyle(name="FCP", fontSize=11, textColor=HexColor("#1a365d"),
                              fontName="Helvetica-Bold", spaceAfter=4))
    styles.add(ParagraphStyle(name="FCR", fontSize=11, leading=15, spaceAfter=12, leftIndent=12))

    story = [Paragraph(escape(titulo), styles["Titulo"])]
    story.append(Paragraph("Resumo", styles["H2"]))
    story.append(Paragraph(escape(resumo), styles["Corpo"]))
    story.append(Paragraph("Conteúdo da aula", styles["H2"]))

    for bloco in conteudo_md.split("\n\n"):
        bloco = bloco.strip()
        if not bloco:
            continue
        if bloco.startswith("# "):
            story.append(Paragraph(escape(bloco[2:]), styles["H2"]))
        elif bloco.startswith("## "):
            story.append(Paragraph(escape(bloco[3:]), styles["H2"]))
        else:
            story.append(Paragraph(escape(bloco), styles["Corpo"]))

    story.append(PageBreak())
    story.append(Paragraph("Flashcards", styles["Titulo"]))
    for i, fc in enumerate(flashcards, 1):
        story.append(Paragraph(f"{i}. {escape(fc['pergunta'])}", styles["FCP"]))
        story.append(Paragraph(escape(fc["resposta"]), styles["FCR"]))

    doc.build(story)
    return pdf_path
