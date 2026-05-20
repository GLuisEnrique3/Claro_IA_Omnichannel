"""
Convierte un .docx a PDF limpio listo para RAG, reformateando con Gemini.
Uso: python test/format_pdf.py [nombre_archivo.docx]
     (por defecto usa el instructivo de contratos OSCAR ACA)
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from docx import Document
from docx.oxml.ns import qn
from fpdf import FPDF
from vertexai.generative_models import Part
from config import llm_model


WORDS_DIR    = Path(__file__).parent.parent / "words"
PDF_DIR      = Path(__file__).parent.parent / "pdf"
#DEFAULT_DOCX = "Copy of CM-IN-05 Instructivo Gestión de contratos con MOLINA ACA.docx"

_SENTINEL = "<<<IMG_{n}>>>"


# ── Imágenes: extracción y descripción con Gemini Vision ──────────────────────

_NS_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
_NS_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_NS_V = 'urn:schemas-microsoft-com:vml'


def _image_from_element(element, doc) -> tuple[bytes, str] | tuple[None, None]:
    """Extrae bytes y mime_type de la primera imagen en un elemento XML."""
    for blip in element.iter(f'{{{_NS_A}}}blip'):
        rId = blip.get(f'{{{_NS_R}}}embed')
        if rId:
            try:
                part = doc.part.related_parts[rId]
                return part.blob, part.content_type
            except Exception:
                pass
    for imgdata in element.iter(f'{{{_NS_V}}}imagedata'):
        rId = imgdata.get(f'{{{_NS_R}}}id')
        if rId:
            try:
                part = doc.part.related_parts[rId]
                return part.blob, part.content_type
            except Exception:
                pass
    return None, None


def _describe_image(img_bytes: bytes, mime_type: str, index: int) -> str:
    """Envía la imagen a Gemini Vision y devuelve una descripción textual."""
    supported = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
    if mime_type not in supported:
        return f"Imagen {index + 1}: formato no soportado ({mime_type})."
    try:
        image_part = Part.from_data(data=img_bytes, mime_type=mime_type)
        response = llm_model.generate_content([
            image_part,
            "Eres un extractor de contenido para un sistema RAG aplicado a documentos instructivos corporativos. "
            "Analiza esta imagen y extrae toda la información útil para responder preguntas operativas. "
            "Sigue estas reglas según el tipo de imagen:\n\n"
            "- DIAGRAMA DE FLUJO O PROCESO: Lista cada paso en orden, indica condiciones (si/no, aprobado/rechazado), "
            "responsables si aparecen, y el resultado final del flujo.\n"
            "- TABLA O FORMULARIO: Transcribe cada campo y su valor o descripción. "
            "Usa el formato 'Campo: Valor'.\n"
            "- PANTALLA O INTERFAZ DE SISTEMA: Describe qué sistema es, qué campos o botones muestra, "
            "y qué acción se está realizando.\n"
            "- TEXTO O INSTRUCTIVO: Transcribe el texto completo con su estructura (títulos, pasos, notas).\n"
            "- IMAGEN DECORATIVA O LOGOTIPO SIN CONTENIDO OPERATIVO: responde solo 'Imagen decorativa.'\n\n"
            "Traduce al español cualquier texto en otro idioma. "
            "No agregues interpretaciones propias. Solo extrae lo que está visible en la imagen. "
            "Redacta en párrafos o listas claras, sin markdown.",
        ])
        return response.text.strip()
    except Exception as exc:
        return f"Imagen {index + 1}: error al describir ({exc})."


# ── Extracción del Word: texto con centinelas + dict de descripciones ──────────

def extract_text(docx_path: Path) -> tuple[str, dict[str, str]]:
    """
    Devuelve (texto_con_centinelas, {centinela: descripción}).
    Las imágenes se reemplazan por <<<IMG_N>>> para que Gemini
    no las toque durante la limpieza.
    """
    doc = Document(docx_path)
    parts = []
    images: dict[str, str] = {}
    img_count = 0

    for child in doc.element.body:
        tag = child.tag.split('}')[-1]

        if tag == 'p':
            img_bytes, mime_type = _image_from_element(child, doc)
            if img_bytes:
                print(f"  [imagen {img_count + 1}] describiendo con Gemini Vision...")
                desc = _describe_image(img_bytes, mime_type, img_count)
                sentinel = _SENTINEL.format(n=img_count)
                images[sentinel] = desc
                parts.append(sentinel)
                img_count += 1

            text = ''.join(
                n.text or '' for n in child.iter() if n.tag.endswith('}t')
            ).strip()
            if text:
                parts.append(text)

        elif tag == 'tbl':
            for row_el in child.iter(qn('w:tr')):
                cells = [
                    ''.join(n.text or '' for n in tc.iter() if n.tag.endswith('}t')).strip()
                    for tc in row_el.findall(qn('w:tc'))
                ]
                row_text = ' | '.join(c for c in cells if c)
                if row_text:
                    parts.append(row_text)

    return '\n\n'.join(parts), images


# ── Limpieza del texto con Gemini (no toca los centinelas) ────────────────────

def clean_with_ai(raw_text: str, filename: str) -> str:
    prompt = (
        f'Eres un editor de documentos técnicos. Recibirás el texto crudo extraído '
        f'del documento Word "{filename}".\n\n'
        'Tu tarea es limpiar y reformatear el texto para que sea óptimo para RAG:\n\n'
        '1. ELIMINA: números de página, encabezados/pies repetitivos, marcas de agua, '
        'artefactos de formato, viñetas duplicadas, líneas en blanco excesivas.\n'
        '2. CONSERVA: todo el contenido informativo, definiciones, procedimientos, '
        'responsabilidades, tablas.\n'
        '3. ESTRUCTURA con este formato:\n'
        '   - Títulos de sección: línea en MAYÚSCULAS sin #\n'
        '   - Párrafos: texto normal\n'
        '   - Listas: "- item"\n'
        '   - Filas de tabla: "Campo | Valor"\n'
        '4. NO uses markdown (#, **, _). Solo texto plano estructurado.\n'
        '5. NO resumas ni agregues texto nuevo. Solo limpia y reorganiza.\n'
        '6. Conserva el español exactamente como está.\n'
        '7. Traduce a español el texto que esté en inglés, conservando el español que ya existe.\n'
        '8. IMPORTANTE: el texto contiene marcadores con formato <<<IMG_N>>> donde N es un '
        'número. Estos indican la posición exacta de una imagen. '
        'Conserva cada marcador EXACTAMENTE en su lugar dentro del flujo del texto. '
        'No los muevas, modifiques ni elimines bajo ninguna circunstancia.\n\n'
        f'Texto crudo:\n{raw_text}\n\n'
        'Devuelve UNICAMENTE el texto limpio, sin comentarios ni explicaciones.'
    )
    response = llm_model.generate_content(prompt)
    return response.text.strip()


# ── Construcción de bloques ordenados: texto e imágenes ───────────────────────

def build_blocks(
    clean_text: str,
    images: dict[str, str],
) -> list[dict]:
    """
    Divide el texto limpio en bloques alternando texto e imagen.
    Cada bloque: {"type": "text"|"image", "content": str, "index": int|None}
    """
    # Patrón que captura los centinelas
    pattern = re.compile(r'(<<<IMG_\d+>>>)')
    segments = pattern.split(clean_text)

    blocks = []
    for seg in segments:
        if not seg.strip():
            continue
        if pattern.fullmatch(seg):
            n = int(re.search(r'\d+', seg).group())
            desc = images.get(seg, f"Imagen {n + 1}: no encontrada.")
            blocks.append({"type": "image", "content": desc, "index": n + 1})
        else:
            blocks.append({"type": "text", "content": seg.strip(), "index": None})

    return blocks


# ── Generación del PDF ────────────────────────────────────────────────────────

class _PDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font('Helvetica', 'I', 8)
        self.cell(0, 6, f'Página {self.page_no()}', align='C')


def _safe(text: str) -> str:
    return text.encode('latin-1', 'replace').decode('latin-1')


def _render_text_block(pdf: FPDF, text: str, page_w: float):
    for raw_line in text.split('\n'):
        line = raw_line.strip()
        pdf.set_x(pdf.l_margin)

        if not line:
            pdf.ln(2)
            continue

        if line.isupper() and len(line) > 4:
            pdf.ln(3)
            pdf.set_font('Helvetica', 'B', 11)
            pdf.multi_cell(page_w, 6, _safe(line))
            continue

        if line.startswith(('- ', '• ', '* ')):
            pdf.set_font('Helvetica', '', 10)
            pdf.multi_cell(page_w, 5, '    ' + _safe(line))
            continue

        if '|' in line:
            pdf.set_font('Helvetica', 'I', 9)
            pdf.multi_cell(page_w, 5, _safe(line))
            continue

        pdf.set_font('Helvetica', '', 10)
        pdf.multi_cell(page_w, 5, _safe(line))


def _render_image_block(pdf: FPDF, desc: str, index: int, page_w: float):
    """Renderiza la descripción de una imagen con caja visual."""
    pdf.set_x(pdf.l_margin)
    pdf.ln(3)

    # Línea superior
    pdf.set_draw_color(120, 120, 120)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + page_w, pdf.get_y())
    pdf.ln(2)

    # Etiqueta
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(page_w, 5, _safe(f'Imagen {index}'))
    pdf.set_text_color(0, 0, 0)

    # Descripción en itálica
    pdf.set_font('Helvetica', 'I', 9)
    for raw_line in desc.split('\n'):
        line = raw_line.strip()
        pdf.set_x(pdf.l_margin)
        if not line:
            pdf.ln(2)
            continue
        pdf.multi_cell(page_w, 5, _safe(line))

    # Línea inferior
    pdf.set_x(pdf.l_margin)
    pdf.ln(2)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + page_w, pdf.get_y())
    pdf.ln(4)


def save_pdf(blocks: list[dict], output_path: Path, title: str):
    pdf = _PDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    page_w = pdf.w - pdf.l_margin - pdf.r_margin

    pdf.set_font('Helvetica', 'B', 13)
    pdf.multi_cell(page_w, 7, _safe(title), align='C')
    pdf.ln(6)

    for block in blocks:
        if block["type"] == "image":
            _render_image_block(pdf, block["content"], block["index"], page_w)
        else:
            _render_text_block(pdf, block["content"], page_w)

    pdf.output(str(output_path))


# ── Punto de entrada ──────────────────────────────────────────────────────────
"""
def main():
    filename = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DOCX
    docx_path = WORDS_DIR / filename

    if not docx_path.exists():
        print(f"Archivo no encontrado: {docx_path}")
        sys.exit(1)

    print(f"Leyendo: {filename}")
    raw_text, images = extract_text(docx_path)
    print(f"  {len(raw_text):,} caracteres | {len(images)} imagenes")

    print("Limpiando texto con Gemini...")
    clean_text = clean_with_ai(raw_text, filename)
    print(f"  {len(clean_text):,} caracteres limpios")

    print("Armando bloques texto/imagen...")
    blocks = build_blocks(clean_text, images)
    text_blocks  = sum(1 for b in blocks if b["type"] == "text")
    image_blocks = sum(1 for b in blocks if b["type"] == "image")
    print(f"  {text_blocks} bloques de texto | {image_blocks} bloques de imagen")

    output_path = PDF_DIR / (docx_path.stem + ".pdf")
    print(f"Generando PDF: {output_path.name}")
    save_pdf(blocks, output_path, docx_path.stem)

    print(f"Guardado en: {output_path}")

if __name__ == "__main__":
    main()

"""

def process_file(docx_path: Path):
    try:
        print("=" * 80)
        print(f"Leyendo: {docx_path.name}")

        raw_text, images = extract_text(docx_path)
        print(f"  {len(raw_text):,} caracteres | {len(images)} imagenes")

        print("Limpiando texto con Gemini...")
        clean_text = clean_with_ai(raw_text, docx_path.name)
        print(f"  {len(clean_text):,} caracteres limpios")

        print("Armando bloques texto/imagen...")
        blocks = build_blocks(clean_text, images)

        text_blocks = sum(1 for b in blocks if b["type"] == "text")
        image_blocks = sum(1 for b in blocks if b["type"] == "image")

        print(f"  {text_blocks} bloques de texto | {image_blocks} bloques de imagen")

        output_path = PDF_DIR / f"{docx_path.stem}.pdf"

        print(f"Generando PDF: {output_path.name}")
        save_pdf(blocks, output_path, docx_path.stem)

        print(f"Guardado en: {output_path}")

    except Exception as e:
        print(f"❌ Error procesando {docx_path.name}: {e}")


def main():

    # Crear carpeta pdf si no existe
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    # Todos los docx de la carpeta
    docx_files = sorted(WORDS_DIR.glob("*.docx"))

    # Ignorar temporales de Word
    docx_files = [
        f for f in docx_files
        if not f.name.startswith("~$")
    ]

    if not docx_files:
        print("No se encontraron archivos .docx")
        return

    print(f"Se encontraron {len(docx_files)} archivos .docx")

    for docx_path in docx_files:

        # OPCIONAL:
        # saltar si el PDF ya existe
        output_pdf = PDF_DIR / f"{docx_path.stem}.pdf"

        if output_pdf.exists():
            print(f"⏭ Saltando {docx_path.name} (PDF ya existe)")
            continue

        process_file(docx_path)

    print("=" * 80)
    print("Proceso finalizado")


if __name__ == "__main__":
    main()
