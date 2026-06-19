import re
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import black
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from app.config import settings

FONT = 'Helvetica'   # equivalente ao Arial em ReportLab
SIZE = 12
LEAD = 18            # espacamento entre linhas


def _normalizar_texto(valor):
    if valor is None:
        return ""
    if isinstance(valor, str):
        return valor
    if isinstance(valor, list):
        partes = []
        for item in valor:
            if isinstance(item, str):
                partes.append(item)
            elif isinstance(item, dict):
                for v in item.values():
                    if isinstance(v, str):
                        partes.append(v)
                        break
            else:
                partes.append(str(item))
        return "\n\n".join(partes)
    if isinstance(valor, dict):
        partes = []
        for k, v in valor.items():
            partes.append(f"{k}\n\n{_normalizar_texto(v)}")
        return "\n\n".join(partes)
    return str(valor)


def _escape(t):
    t = str(t) if t else ""
    for char in ['*', '#', '@', '$', '_']:
        t = t.replace(char, '')
    t = t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return t


def gerar_pdf(aula_id, titulo, transcricao_bruta, estruturado, flashcards):
    pdf_path = str(settings.pdf_dir / f"aula_{aula_id}.pdf")
    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        topMargin=2*cm, bottomMargin=2*cm,
        leftMargin=2.5*cm, rightMargin=2.5*cm
    )

    # Um unico estilo: Helvetica 12, alinhamento esquerdo, sem bold/italic/cores
    normal = ParagraphStyle(
        name='Normal',
        fontName=FONT,
        fontSize=SIZE,
        leading=LEAD,
        textColor=black,
        spaceAfter=8,
        spaceBefore=0,
    )
    titulo_style = ParagraphStyle(
        name='Titulo',
        fontName=FONT,
        fontSize=SIZE,
        leading=LEAD,
        textColor=black,
        spaceAfter=4,
        spaceBefore=0,
    )
    secao_style = ParagraphStyle(
        name='Secao',
        fontName=FONT,
        fontSize=SIZE,
        leading=LEAD,
        textColor=black,
        spaceAfter=4,
        spaceBefore=12,
    )

    transcricao_bruta = _normalizar_texto(transcricao_bruta)

    def add_text(content, story):
        content = _normalizar_texto(content)
        if not content:
            return
        for block in content.split('\n\n'):
            block = block.strip()
            if not block:
                continue
            # Remove prefixos markdown — tudo vira texto plano
            clean = re.sub(r'^#{1,6}\s*', '', block)
            clean = re.sub(r'\*\*(.*?)\*\*', r'\1', clean)
            clean = re.sub(r'\*(.*?)\*', r'\1', clean)
            clean = re.sub(r'^[-*]\s+', '  ', clean, flags=re.MULTILINE)
            texto = ' '.join(l.strip() for l in clean.split('\n') if l.strip())
            if texto:
                story.append(Paragraph(_escape(texto), normal))

    story = []

    # Titulo
    story.append(Paragraph(_escape(titulo), titulo_style))
    story.append(Spacer(1, 6))

    # Guia de estudos
    story.append(Paragraph('APRESENTACAO DO TEMA E GUIA DE ESTUDOS', secao_style))
    add_text(estruturado.get('guia_de_estudos', ''), story)
    story.append(PageBreak())

    # Resumo
    story.append(Paragraph('RESUMO EXPANDIDO DA AULA', secao_style))
    add_text(estruturado.get('resumo_expandido', ''), story)
    story.append(PageBreak())

    # Palacio mental (se houver)
    palacio = estruturado.get('palacio_mental', '')
    if palacio and palacio.strip():
        story.append(Paragraph('PALACIO MENTAL DA MATERIA', secao_style))
        add_text(palacio, story)
        story.append(PageBreak())

    # Transcricao
    story.append(Paragraph('TRANSCRICAO EXATA DA AULA', secao_style))
    for bloco in transcricao_bruta.split('\n\n'):
        if bloco.strip():
            story.append(Paragraph(_escape(bloco.strip()), normal))
    story.append(PageBreak())

    # Flashcards
    story.append(Paragraph('FLASHCARDS', secao_style))
    for i, fc in enumerate(flashcards, 1):
        story.append(Paragraph(f'{i}. {_escape(fc["pergunta"])}', normal))
        story.append(Paragraph(_escape(fc['resposta']), normal))
        story.append(Spacer(1, 4))

    doc.build(story)
    return pdf_path
