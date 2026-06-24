import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from docx import Document
from docx.oxml.ns import qn
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)
from vertexai.generative_models import Part

from config import llm_model


WORDS_DIR = Path(__file__).parent.parent / "words"
PDF_DIR = Path(__file__).parent.parent / "pdf"

_SENTINEL = "<<<IMG_{n}>>>"

_BLUE_DARK   = HexColor("#003366")
_BLUE_ACCENT = HexColor("#0055AA")
_GRAY_BORDER = HexColor("#CCCCCC")

_FONT = "Helvetica"  # updated by _register_fonts()

# ─────────────────────────────────────────────────────────────
# Namespaces DOCX
# ─────────────────────────────────────────────────────────────

_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_V = "urn:schemas-microsoft-com:vml"

# ─────────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────────


def normalize_sentinels(text: str) -> str:
    return re.sub(r"\s*(<<<IMG_\d+>>>)\s*", r"\n\n\1\n\n", text)


def _escape(text: str) -> str:
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ─────────────────────────────────────────────────────────────
# EXTRACCIÓN DE IMÁGENES
# ─────────────────────────────────────────────────────────────


def _image_from_element(element, doc):
    for blip in element.iter(f"{{{_NS_A}}}blip"):
        rId = blip.get(f"{{{_NS_R}}}embed")
        if rId:
            try:
                part = doc.part.related_parts[rId]
                return part.blob, part.content_type
            except Exception:
                pass
    for imgdata in element.iter(f"{{{_NS_V}}}imagedata"):
        rId = imgdata.get(f"{{{_NS_R}}}id")
        if rId:
            try:
                part = doc.part.related_parts[rId]
                return part.blob, part.content_type
            except Exception:
                pass
    return None, None


# ─────────────────────────────────────────────────────────────
# GEMINI VISION
# ─────────────────────────────────────────────────────────────


def _describe_image(img_bytes: bytes, mime_type: str, index: int) -> str:
    supported = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if mime_type not in supported:
        return f"Contenido visual no soportado ({mime_type})"
    try:
        image_part = Part.from_data(data=img_bytes, mime_type=mime_type)
        response = llm_model.generate_content([
            image_part,
            """
Eres un analizador experto de documentos corporativos para sistemas RAG.

Tu objetivo NO es describir visualmente la imagen.

Tu objetivo es convertir el contenido visual en conocimiento operativo útil.

REGLAS:

1. Si la imagen es un FLUJO o DIAGRAMA:
- Convierte el flujo en una explicación narrativa paso a paso.
- Explica cómo inicia el proceso.
- Explica validaciones, decisiones y resultados.
- Explica qué sistemas participan.
- Explica responsables si existen.
- Explica condiciones SLA o tiempos si aparecen.
- NO describas cajas, flechas o formas visuales.

2. Si la imagen es una TABLA:
- Reorganiza la información en texto estructurado.
- Usa:
Campo | Valor

3. Si la imagen es una PANTALLA:
- Explica qué operación realiza el usuario.
- Explica campos importantes.
- Explica el propósito de la pantalla.

4. Si la imagen contiene TEXTO:
- Reescribe el contenido de forma limpia y estructurada.
- Elimina ruido visual.

5. Si la imagen es decorativa:
Responde únicamente:
Imagen decorativa.

IMPORTANTE:
- Redacta SIEMPRE en español.
- Prioriza utilidad semántica para búsquedas RAG.
- NO hables de elementos visuales.
- NO digas "en la imagen aparece".
- NO enumeres colores, cajas o flechas.
- Convierte el contenido en documentación operativa real.
"""
        ])
        return response.text.strip()
    except Exception as exc:
        return f"Error interpretando contenido visual: {exc}"


# ─────────────────────────────────────────────────────────────
# EXTRACCIÓN DOCX
# ─────────────────────────────────────────────────────────────


def extract_text(docx_path: Path):
    doc = Document(docx_path)
    parts = []
    images = {}
    img_count = 0

    for child in doc.element.body:
        tag = child.tag.split("}")[-1]

        if tag == "p":
            img_bytes, mime_type = _image_from_element(child, doc)
            if img_bytes:
                print(f"  [imagen {img_count + 1}] analizando...")
                desc = _describe_image(img_bytes, mime_type, img_count)
                sentinel = _SENTINEL.format(n=img_count)
                images[sentinel] = desc
                parts.append(sentinel)
                img_count += 1
            text = "".join(
                n.text or "" for n in child.iter() if n.tag.endswith("}t")
            ).strip()
            if text:
                parts.append(text)

        elif tag == "tbl":
            for row_el in child.iter(qn("w:tr")):
                cells = [
                    "".join(
                        n.text or "" for n in tc.iter() if n.tag.endswith("}t")
                    ).strip()
                    for tc in row_el.findall(qn("w:tc"))
                ]
                row_text = " | ".join(c for c in cells if c)
                if row_text:
                    parts.append(row_text)

    return "\n\n".join(parts), images


# ─────────────────────────────────────────────────────────────
# LIMPIEZA CON GEMINI
# ─────────────────────────────────────────────────────────────


def clean_with_ai(raw_text: str, filename: str):
    prompt = f"""
Eres un editor especializado en documentación corporativa para sistemas RAG.

Recibirás texto extraído automáticamente desde:
{filename}

OBJETIVO:
Transformar el contenido en documentación limpia,
estructurada y semánticamente útil.

INSTRUCCIONES:

1. ELIMINAR:
- números de página
- encabezados repetidos
- pies de página
- ruido visual
- líneas vacías excesivas
- caracteres dañados
- texto duplicado

2. CONSERVAR:
- procesos
- definiciones
- validaciones
- reglas
- SLA
- tablas
- pasos operativos
- responsabilidades
- flujos
- configuraciones

3. FORMATO:
- títulos en MAYÚSCULAS
- párrafos limpios
- listas usando:
  - item
- tablas usando:
  Campo | Valor

4. NO uses markdown.

5. NO inventes información.

6. NO resumas demasiado.

7. Convierte texto en inglés a español.

8. MUY IMPORTANTE:
Existen marcadores EXACTOS con formato:
<<<IMG_N>>>

Debes conservarlos EXACTAMENTE igual.
NO eliminarlos.
NO moverlos.
NO modificarlos.

9. Si un flujo está mal fragmentado,
reorganízalo en narrativa coherente.

10. Prioriza claridad semántica para embeddings.

TEXTO:

{raw_text}

Devuelve únicamente el texto limpio.
"""
    response = llm_model.generate_content(prompt)
    clean_text = response.text.strip()
    clean_text = normalize_sentinels(clean_text)
    return clean_text


# ─────────────────────────────────────────────────────────────
# BLOQUES
# ─────────────────────────────────────────────────────────────


def build_blocks(clean_text: str, images: dict):
    pattern = re.compile(r"<<<IMG_(\d+)>>>")
    segments = pattern.split(clean_text)
    blocks = []
    i = 0
    while i < len(segments):
        seg = segments[i].strip()
        if not seg:
            i += 1
            continue
        if i % 2 == 0:
            blocks.append({"type": "text", "content": seg})
        else:
            sentinel = f"<<<IMG_{seg}>>>"
            desc = images.get(sentinel, "")
            if desc and desc != "Imagen decorativa.":
                blocks.append({"type": "image", "content": desc})
        i += 1
    return blocks


# ─────────────────────────────────────────────────────────────
# PDF – FUENTES
# ─────────────────────────────────────────────────────────────


def _register_fonts():
    global _FONT
    fonts_dir = Path("C:/Windows/Fonts")
    try:
        pdfmetrics.registerFont(TTFont("Arial", str(fonts_dir / "arial.ttf")))
        pdfmetrics.registerFont(TTFont("Arial-Bold", str(fonts_dir / "arialbd.ttf")))
        pdfmetrics.registerFont(TTFont("Arial-Italic", str(fonts_dir / "ariali.ttf")))
        pdfmetrics.registerFont(TTFont("Arial-BoldItalic", str(fonts_dir / "arialbi.ttf")))
        _FONT = "Arial"
    except Exception:
        _FONT = "Helvetica"
    return _FONT


# ─────────────────────────────────────────────────────────────
# PDF – ESTILOS
# ─────────────────────────────────────────────────────────────


def _build_styles(font_name: str) -> dict:
    if font_name == "Arial":
        bold       = "Arial-Bold"
        italic     = "Arial-Italic"
        bold_italic = "Arial-BoldItalic"
    else:
        bold       = "Helvetica-Bold"
        italic     = "Helvetica-Oblique"
        bold_italic = "Helvetica-BoldOblique"

    return {
        "Title": ParagraphStyle(
            "DocTitle",
            fontName=bold,
            fontSize=16,
            textColor=_BLUE_DARK,
            spaceAfter=6,
            alignment=1,
        ),
        "Heading": ParagraphStyle(
            "H2",
            fontName=bold,
            fontSize=11,
            textColor=_BLUE_DARK,
            spaceBefore=8,
            spaceAfter=2,
            leading=15,
        ),
        "Body": ParagraphStyle(
            "Body",
            fontName=font_name,
            fontSize=10,
            spaceAfter=3,
            leading=14,
        ),
        "ListItem": ParagraphStyle(
            "ListItem",
            fontName=font_name,
            fontSize=10,
            leftIndent=18,
            spaceAfter=2,
            leading=13,
        ),
        "TableRow": ParagraphStyle(
            "TableRow",
            fontName=italic,
            fontSize=9,
            spaceAfter=2,
            leading=12,
        ),
        "ImageLabel": ParagraphStyle(
            "ImageLabel",
            fontName=bold_italic,
            fontSize=9,
            textColor=_BLUE_ACCENT,
            spaceAfter=3,
        ),
        "ImageBody": ParagraphStyle(
            "ImageBody",
            fontName=italic,
            fontSize=9,
            textColor=colors.Color(0.25, 0.25, 0.25),
            spaceAfter=2,
            leading=13,
        ),
    }


# ─────────────────────────────────────────────────────────────
# PDF – RENDER TEXTO
# ─────────────────────────────────────────────────────────────


def _text_to_story(text: str, styles: dict, story: list):
    for raw_line in text.split("\n"):
        line = raw_line.strip()

        if not line:
            story.append(Spacer(1, 3))
            continue

        # Heading: all-caps line longer than 4 chars
        if line.isupper() and len(line) > 4:
            story.append(Spacer(1, 4))
            story.append(Paragraph(_escape(line), styles["Heading"]))
            story.append(HRFlowable(
                width="100%",
                thickness=0.5,
                color=_BLUE_DARK,
                spaceAfter=3,
            ))
            continue

        # List item
        if line.startswith(("- ", "• ", "* ")):
            story.append(Paragraph(
                "&#8226;&nbsp;&nbsp;" + _escape(line[2:]),
                styles["ListItem"],
            ))
            continue

        # Table row (pipe-separated)
        if "|" in line:
            story.append(Paragraph(_escape(line), styles["TableRow"]))
            continue

        story.append(Paragraph(_escape(line), styles["Body"]))


# ─────────────────────────────────────────────────────────────
# PDF – RENDER IMAGEN
# ─────────────────────────────────────────────────────────────


def _image_to_story(desc: str, styles: dict, story: list):
    story.append(Spacer(1, 4))
    story.append(Paragraph("[IMAGEN]", styles["ImageLabel"]))
    for raw_line in desc.strip().split("\n"):
        line = raw_line.strip()
        if line:
            story.append(Paragraph(_escape(line), styles["ImageBody"]))
        else:
            story.append(Spacer(1, 3))
    story.append(HRFlowable(width="100%", thickness=0.3, color=_GRAY_BORDER, spaceAfter=6))


# ─────────────────────────────────────────────────────────────
# PDF – PIE DE PÁGINA
# ─────────────────────────────────────────────────────────────


def _page_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(_FONT, 8)
    canvas.setFillColor(colors.grey)
    canvas.drawCentredString(letter[0] / 2, 0.75 * cm, f"Página {doc.page}")
    canvas.restoreState()


# ─────────────────────────────────────────────────────────────
# PDF – SAVE
# ─────────────────────────────────────────────────────────────


def save_pdf(blocks: list, output_path: Path, title: str):
    font_name = _register_fonts()
    styles = _build_styles(font_name)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=title,
    )

    story = []
    story.append(Paragraph(_escape(title), styles["Title"]))
    story.append(HRFlowable(
        width="100%",
        thickness=1.5,
        color=_BLUE_DARK,
        spaceAfter=10,
    ))

    for block in blocks:
        if block["type"] == "image":
            _image_to_story(block["content"], styles, story)
        else:
            _text_to_story(block["content"], styles, story)

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)


# ─────────────────────────────────────────────────────────────
# PROCESAMIENTO
# ─────────────────────────────────────────────────────────────


def process_file(docx_path: Path):
    try:
        print("=" * 80)
        print(f"Leyendo: {docx_path.name}")

        raw_text, images = extract_text(docx_path)
        print(f"  {len(raw_text):,} caracteres")
        print(f"  {len(images)} imagenes")

        print("Limpiando con Gemini...")
        clean_text = clean_with_ai(raw_text, docx_path.name)
        print(f"  {len(clean_text):,} caracteres limpios")

        print("Construyendo bloques...")
        blocks = build_blocks(clean_text, images)

        text_blocks  = sum(1 for b in blocks if b["type"] == "text")
        image_blocks = sum(1 for b in blocks if b["type"] == "image")
        print(f"  {text_blocks} bloques texto")
        print(f"  {image_blocks} bloques imagen")

        output_path = PDF_DIR / f"{docx_path.stem}.pdf"
        print(f"Generando PDF: {output_path.name}")

        save_pdf(blocks, output_path, docx_path.stem)
        print(f"✅ Guardado en: {output_path}")

    except Exception as e:
        print(f"❌ Error procesando {docx_path.name}: {e}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────


def main():
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    docx_files = sorted(WORDS_DIR.glob("*.docx"))
    docx_files = [f for f in docx_files if not f.name.startswith("~$")]

    if not docx_files:
        print("No se encontraron archivos .docx")
        return

    print(f"Se encontraron {len(docx_files)} archivos")

    for docx_path in docx_files:
        output_pdf = PDF_DIR / f"{docx_path.stem}.pdf"
        if output_pdf.exists():
            print(f"⏭ Saltando {docx_path.name}")
            continue
        process_file(docx_path)

    print("=" * 80)
    print("Proceso finalizado")


if __name__ == "__main__":
    main()
