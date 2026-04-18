import collections
import re
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import black, red, orange
from reportlab.platypus import SimpleDocTemplate, Paragraph, PageBreak
from app.config import settings


def _normalizar_texto(valor):
    """Aceita string, lista ou dict e retorna sempre uma string."""
    if valor is None:
        return ""
    if isinstance(valor, str):
        return valor
    if isinstance(valor, list):
        # Lista de strings ou dicts
        partes = []
        for item in valor:
            if isinstance(item, str):
                partes.append(item)
            elif isinstance(item, dict):
                # Pega o primeiro valor string do dict
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
            partes.append(f"## {k}\n\n{_normalizar_texto(v)}")
        return "\n\n".join(partes)
    return str(valor)


def _analisar_frequencia(transcricao_bruta: str):
    palavras = re.findall(r'\w+', transcricao_bruta.lower())
    stop_words = {'a', 'o', 'e', 'do', 'da', 'em', 'um', 'uma', 'que', 'com', 'no', 'na', 'para', 'os', 'as', 'de', 'se', 'por', 'mais', 'sua', 'seu', 'como', 'mas', 'foi', 'ser', 'são', 'tem', 'ter', 'ele', 'ela', 'isso', 'esse', 'essa', 'esta', 'este'}
    contagem = collections.Counter(p for p in palavras if p not in stop_words and len(p) > 3)
    top = [item[0] for item in contagem.most_common(20)]
    return set(top[:5]), set(top[5:15])


def escape(t):
    t = str(t) if t else ""
    for char in ['*', '#', '@', '$', '_']:
        t = t.replace(char, '')
    t = t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return t


def gerar_pdf(aula_id, titulo, transcricao_bruta, estruturado, flashcards):
    pdf_path = str(settings.pdf_dir / f"aula_{aula_id}.pdf")
    doc = SimpleDocTemplate(pdf_path, pagesize=A4, topMargin=2*cm, bottomMargin=2*cm,
                            leftMargin=2.5*cm, rightMargin=2.5*cm)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Titulo', fontSize=20, textColor=black, spaceBefore=2*cm,
                              spaceAfter=18, fontName='Times-Bold'))
    styles.add(ParagraphStyle(name='H2_Base', fontSize=14, spaceAfter=12, spaceBefore=18,
                              fontName='Times-BoldItalic'))
    styles.add(ParagraphStyle(name='H3_Base', fontSize=12, spaceAfter=10, spaceBefore=12,
                              fontName='Times-BoldItalic'))
    styles.add(ParagraphStyle(name='Corpo', fontSize=12, leading=20, spaceAfter=16,
                              fontName='Times-Roman'))
    styles.add(ParagraphStyle(name='FCP', fontSize=12, textColor=black, fontName='Times-Bold',
                              spaceAfter=6, leading=20))
    styles.add(ParagraphStyle(name='FCR', fontSize=12, leading=20, spaceAfter=16,
                              leftIndent=12, fontName='Times-Roman'))

    transcricao_bruta = _normalizar_texto(transcricao_bruta)
    muito_rep, rel_rep = _analisar_frequencia(transcricao_bruta)

    def get_color(texto):
        tl = texto.lower()
        if any(w in tl for w in muito_rep):
            return red
        if any(w in tl for w in rel_rep):
            return orange
        return black

    def add_content(content, story, force_body=False):
        content = _normalizar_texto(content)
        if not content:
            return
        for block in content.split('\n\n'):
            block = block.strip()
            if not block:
                continue
            if force_body:
                clean = block.replace('### ', '').replace('## ', '').replace('# ', '')
                story.append(Paragraph(escape(clean), styles['Corpo']))
            elif block.startswith('### '):
                style = ParagraphStyle(name='H3d', parent=styles['H3_Base'], textColor=get_color(block))
                story.append(Paragraph(escape(block[4:]), style))
            elif block.startswith('## '):
                style = ParagraphStyle(name='H2d', parent=styles['H2_Base'], textColor=get_color(block))
                story.append(Paragraph(escape(block[3:]), style))
            elif block.startswith('# '):
                story.append(Paragraph(escape(block[2:]), styles['Titulo']))
            elif block.startswith('* ') or block.startswith('- '):
                for line in block.split('\n'):
                    if line.strip():
                        story.append(Paragraph(escape(f'  • {line.strip("* ").strip("- ")}'), styles['Corpo']))
            else:
                texto = ' '.join(l.strip() for l in block.split('\n'))
                story.append(Paragraph(escape(texto), styles['Corpo']))

    story = [Paragraph(escape(titulo), styles['Titulo'])]

    story.append(Paragraph('APRESENTAÇÃO DO TEMA E GUIA DE ESTUDOS', styles['H2_Base']))
    add_content(estruturado.get('guia_de_estudos', ''), story)
    story.append(PageBreak())

    story.append(Paragraph('RESUMO EXPANDIDO DA AULA', styles['H2_Base']))
    add_content(estruturado.get('resumo_expandido', ''), story)
    story.append(PageBreak())

    story.append(Paragraph('PALÁCIO MENTAL DA MATÉRIA', styles['H2_Base']))
    add_content(estruturado.get('palacio_mental', ''), story, force_body=True)
    story.append(PageBreak())

    story.append(Paragraph('TRANSCRIÇÃO EXATA DA AULA', styles['H2_Base']))
    for bloco in transcricao_bruta.split('\n\n'):
        if bloco.strip():
            story.append(Paragraph(escape(bloco.strip()), styles['Corpo']))
    story.append(PageBreak())

    story.append(Paragraph('FLASHCARDS', styles['H2_Base']))
    for i, fc in enumerate(flashcards, 1):
        story.append(Paragraph(f'{i}. {escape(fc["pergunta"])}', styles['FCP']))
        story.append(Paragraph(escape(fc['resposta']), styles['FCR']))

    doc.build(story)
    return pdf_path
