"""
Convierte documentos .docx a PDFs optimizados para RAG utilizando Gemini.
- Extrae texto
- Interpreta imágenes/diagramas/tablas
- Convierte diagramas en narrativa operativa
- Limpia estructura
- Genera PDF semántico limpio

Uso:
    python test/format_pdf.py
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


WORDS_DIR = Path(__file__).parent.parent / "words"
PDF_DIR = Path(__file__).parent.parent / "pdf"

_SENTINEL = "<<<IMG_{n}>>>"

# ─────────────────────────────────────────────────────────────
# Namespaces DOCX
# ─────────────────────────────────────────────────────────────

_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_V = "urn:schemas-microsoft-com:vml"

# ─────────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────────


def _safe(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


def normalize_sentinels(text: str) -> str:
    """
    Normaliza los centinelas para evitar que Gemini
    rompa el patrón por espacios o saltos.
    """

    return re.sub(
        r"\s*(<<<IMG_\d+>>>)\s*",
        r"\n\n\1\n\n",
        text,
    )


# ─────────────────────────────────────────────────────────────
# EXTRACCIÓN DE IMÁGENES
# ─────────────────────────────────────────────────────────────


def _image_from_element(element, doc):
    """
    Extrae bytes y mime_type de la primera imagen
    dentro de un elemento XML.
    """

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

    supported = {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
    }

    if mime_type not in supported:
        return f"Contenido visual no soportado ({mime_type})"

    try:

        image_part = Part.from_data(
            data=img_bytes,
            mime_type=mime_type,
        )

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
- NO digas “en la imagen aparece”.
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

        # ─────────────────────────────
        # PÁRRAFOS
        # ─────────────────────────────

        if tag == "p":

            img_bytes, mime_type = _image_from_element(child, doc)

            if img_bytes:

                print(
                    f"  [imagen {img_count + 1}] analizando..."
                )

                desc = _describe_image(
                    img_bytes,
                    mime_type,
                    img_count,
                )

                sentinel = _SENTINEL.format(n=img_count)

                images[sentinel] = desc

                parts.append(sentinel)

                img_count += 1

            text = "".join(
                n.text or ""
                for n in child.iter()
                if n.tag.endswith("}t")
            ).strip()

            if text:
                parts.append(text)

        # ─────────────────────────────
        # TABLAS
        # ─────────────────────────────

        elif tag == "tbl":

            for row_el in child.iter(qn("w:tr")):

                cells = [
                    "".join(
                        n.text or ""
                        for n in tc.iter()
                        if n.tag.endswith("}t")
                    ).strip()
                    for tc in row_el.findall(qn("w:tc"))
                ]

                row_text = " | ".join(
                    c for c in cells if c
                )

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

        # Texto normal
        if i % 2 == 0:

            blocks.append({
                "type": "text",
                "content": seg,
            })

        # Imagen
        else:

            sentinel = f"<<<IMG_{seg}>>>"

            desc = images.get(
                sentinel,
                ""
            )

            if desc and desc != "Imagen decorativa.":

                blocks.append({
                    "type": "image",
                    "content": desc,
                })

        i += 1

    return blocks


# ─────────────────────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────────────────────


class _PDF(FPDF):

    def footer(self):

        self.set_y(-12)

        self.set_font("Helvetica", "I", 8)

        self.cell(
            0,
            6,
            f"Página {self.page_no()}",
            align="C",
        )


# ─────────────────────────────────────────────────────────────
# RENDER TEXTO
# ─────────────────────────────────────────────────────────────


def _render_text_block(
    pdf: FPDF,
    text: str,
    page_w: float,
):

    for raw_line in text.split("\n"):

        line = raw_line.strip()

        pdf.set_x(pdf.l_margin)

        if not line:
            pdf.ln(2)
            continue

        # ─────────────────────────────
        # TÍTULOS
        # ─────────────────────────────

        if line.isupper() and len(line) > 4:

            pdf.ln(3)

            pdf.set_font(
                "Helvetica",
                "B",
                11,
            )

            pdf.multi_cell(
                page_w,
                6,
                _safe(line),
            )

            pdf.ln(1)

            continue

        # ─────────────────────────────
        # LISTAS
        # ─────────────────────────────

        if line.startswith(("- ", "• ", "* ")):

            pdf.set_font(
                "Helvetica",
                "",
                10,
            )

            pdf.multi_cell(
                page_w,
                5,
                "   " + _safe(line),
            )

            continue

        # ─────────────────────────────
        # TABLAS
        # ─────────────────────────────

        if "|" in line:

            pdf.set_font(
                "Helvetica",
                "I",
                9,
            )

            pdf.multi_cell(
                page_w,
                5,
                _safe(line),
            )

            continue

        # ─────────────────────────────
        # TEXTO NORMAL
        # ─────────────────────────────

        pdf.set_font(
            "Helvetica",
            "",
            10,
        )

        pdf.multi_cell(
            page_w,
            5,
            _safe(line),
        )


# ─────────────────────────────────────────────────────────────
# RENDER IMÁGENES
# ─────────────────────────────────────────────────────────────


def _render_image_block(
    pdf: FPDF,
    desc: str,
    page_w: float,
):

    pdf.ln(2)

    pdf.set_font(
        "Helvetica",
        "I",
        9,
    )

    for raw_line in desc.split("\n"):

        line = raw_line.strip()

        if not line:
            pdf.ln(2)
            continue

        pdf.multi_cell(
            page_w,
            5,
            _safe(line),
        )

    pdf.ln(3)


# ─────────────────────────────────────────────────────────────
# SAVE PDF
# ─────────────────────────────────────────────────────────────


def save_pdf(
    blocks: list,
    output_path: Path,
    title: str,
):

    pdf = _PDF()

    pdf.set_auto_page_break(
        auto=True,
        margin=15,
    )

    pdf.add_page()

    page_w = (
        pdf.w
        - pdf.l_margin
        - pdf.r_margin
    )

    # ─────────────────────────────
    # TÍTULO
    # ─────────────────────────────

    pdf.set_font(
        "Helvetica",
        "B",
        13,
    )

    pdf.multi_cell(
        page_w,
        7,
        _safe(title),
        align="C",
    )

    pdf.ln(6)

    # ─────────────────────────────
    # BLOQUES
    # ─────────────────────────────

    for block in blocks:

        if block["type"] == "image":

            _render_image_block(
                pdf,
                block["content"],
                page_w,
            )

        else:

            _render_text_block(
                pdf,
                block["content"],
                page_w,
            )

    pdf.output(str(output_path))


# ─────────────────────────────────────────────────────────────
# PROCESAMIENTO
# ─────────────────────────────────────────────────────────────


def process_file(docx_path: Path):

    try:

        print("=" * 80)
        print(f"Leyendo: {docx_path.name}")

        raw_text, images = extract_text(docx_path)

        print(
            f"  {len(raw_text):,} caracteres"
        )

        print(
            f"  {len(images)} imagenes"
        )

        print("Limpiando con Gemini...")

        clean_text = clean_with_ai(
            raw_text,
            docx_path.name,
        )

        print(
            f"  {len(clean_text):,} caracteres limpios"
        )

        print("Construyendo bloques...")

        blocks = build_blocks(
            clean_text,
            images,
        )

        text_blocks = sum(
            1 for b in blocks
            if b["type"] == "text"
        )

        image_blocks = sum(
            1 for b in blocks
            if b["type"] == "image"
        )

        print(
            f"  {text_blocks} bloques texto"
        )

        print(
            f"  {image_blocks} bloques imagen"
        )

        output_path = (
            PDF_DIR /
            f"{docx_path.stem}.pdf"
        )

        print(
            f"Generando PDF: {output_path.name}"
        )

        save_pdf(
            blocks,
            output_path,
            docx_path.stem,
        )

        print(
            f"✅ Guardado en: {output_path}"
        )

    except Exception as e:

        print(
            f"❌ Error procesando {docx_path.name}: {e}"
        )


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────


def main():

    PDF_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    docx_files = sorted(
        WORDS_DIR.glob("*.docx")
    )

    docx_files = [
        f for f in docx_files
        if not f.name.startswith("~$")
    ]

    if not docx_files:

        print(
            "No se encontraron archivos .docx"
        )

        return

    print(
        f"Se encontraron {len(docx_files)} archivos"
    )

    for docx_path in docx_files:

        output_pdf = (
            PDF_DIR /
            f"{docx_path.stem}.pdf"
        )

        # Saltar si ya existe
        if output_pdf.exists():

            print(
                f"⏭ Saltando {docx_path.name}"
            )

            continue

        process_file(docx_path)

    print("=" * 80)
    print("Proceso finalizado")


if __name__ == "__main__":
    main()