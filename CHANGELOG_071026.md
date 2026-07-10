# Changelog — 2026-07-10

Sesión enfocada en ampliar el catálogo **C — Documentos Normativos** de `data/use_cases.json`
con 7 nuevos casos de uso tipo FAQ (antes concentrados en una sola entrada genérica),
reubicar la Guía de Reconciliaciones desde el catálogo B, y registrar sus PDFs fuente en
`scripts/precalcular_embeddings.py`.

---

## 1. Catálogo C — FAQ desglosadas (`data/use_cases.json`)

La entrada `id: "4"` ("CLARO INSURANCE FAQ") era un cajón de sastre con ~90
`semantic_examples` cubriendo temas muy distintos (onboarding, comisiones, licencias,
soporte técnico, estructura de agencia, etc.), todos apuntando al mismo
`metadata_filter.category: "claro insurance faq"`. Se dividió en 7 casos de uso
independientes, cada uno con su propia categoría de metadata para poder filtrar el
documento correcto en Chroma:

| id | `texto` | `metadata_filter.category` |
|---|---|---|
| 4 | FAQ_Informacion_General_Claro_Insurance | `faq_informacion_general` |
| 5 | FAQ_Liberacion_y_Terminacion_de_Contratos | `faq_liberacion_contratos` |
| 6 | FAQ_Licencias_y_Credenciales | `faq_licencias` |
| 7 | FAQ_Impacto_Regulatorio_y_Cambios_Operativos | `faq_impactos_regulatorios` |
| 8 | FAQ_Soporte_Tecnico_y_Acceso_a_Portales | `faq_soporte_tecnico_acceso_portales` |
| 9 | FAQ_Visibilidad_de_Clientes_y_Polizas_en_Plataforma | `faq_visibilidad_clientes_polizas` |
| 10 | FAQ_Gestion_de_Agentes_y_Estructura_de_Agencia | `faq_gestion_agentes_estructura_agencia` |

Todos comparten `coleccion_chroma: "documentos_normativos"` y `tipo: "rag"`. Cada uno
incluye `usa_esto_cuando` redactado a la medida del tema para que el clasificador LLM
pueda diferenciarlos.

**Ronda de calidad posterior:** las 7 entradas se crearon inicialmente con un solo
`semantic_example` (idéntico al `texto`, ej. solo la cadena
`"FAQ_Informacion_General_Claro_Insurance"`) y el mismo `ending_resolution` genérico
copiado y pegado en las 7. Se corrigió:
- Se agregaron **5 `semantic_examples` realistas** por caso (preguntas parafraseadas
  como las escribiría un agente), en vez de un placeholder igual al `texto`.
- Se redactó un **`ending_resolution` distinto y contextual** por cada caso (antes:
  "Si deseas conocer más sobre algún servicio específico de Claro Insurance..." repetido
  7 veces sin variación).

También se corrigió el `metadata_filter.category` de `id: "3"` (ARC Off-Exchange FAQ) de
`"faq"` a `"faq_arc"`, para alinearlo con la nueva convención de nombres de categoría.

---

## 2. Reubicación: Guía de Reconciliaciones (B → C)

La entrada `id: "11"` del catálogo **B** ("Guía de Reconciliaciones", la guía extensa de
elegibilidad/estados/plazos de reconciliación de comisiones) apuntaba a
`coleccion_chroma: "reconciliaciones_faq"`, una colección propia separada de
`documentos_normativos`. Se movió a **C** como `id: "11"` (catálogo C pasó de 10 a 11
entradas), conservando íntegro su contenido (`semantic_examples`, `entity_resolution`
extenso con las 9 reglas de negocio, `usa_esto_cuando`), pero:

- `coleccion_chroma`: `"reconciliaciones_faq"` → `"documentos_normativos"`
- Se agregó `metadata_filter: { "category": "faq_reconciliaciones" }` (antes no tenía
  `metadata_filter`, filtraba por colección completa)

**Motivo:** consolidar todo el contenido normativo/FAQ en una sola colección
(`documentos_normativos`) filtrada por categoría, en vez de mantener colecciones ad-hoc
por documento. El catálogo B queda con un salto en la numeración (`...10, 12, 13...`),
sin impacto funcional — el lookup de casos de uso es por `id` dentro de cada catálogo,
no requiere consecutividad.

---

## 3. Nuevos documentos fuente (`scripts/precalcular_embeddings.py`)

Se actualizó la lista `DOCUMENTS` para reflejar los cambios anteriores:

- **Renombrados** (mismo contenido, nuevo nombre de archivo y categoría):
  - `ARC_OFF_EXCHANGES_FAQ.pdf` → `FAQ_ARC_OFF_EXCHANGES.pdf` (`faq` → `faq_arc`)
- **Eliminados** de `DOCUMENTS` (reemplazados por las entradas desglosadas de abajo):
  - `CLARO_INSURANCE_FAQ.pdf` (`category: "claro insurance faq"`)
  - `Guia_Reconciliaciones_Cliente_RAG.pdf` (`collection: "reconciliaciones_faq"`)
- **Agregados**, todos con `collection: "documentos_normativos"` y `chunking: "sliding"`:
  - `FAQ_reconciliaciones.pdf` → `faq_reconciliaciones`
  - `FAQ_Informacion_General_Claro_Insurance.pdf` → `faq_informacion_general`
  - `FAQ_Liberacion_y_Terminacion_de_Contratos.pdf` → `faq_liberacion_contratos`
  - `FAQ_Licencias_y_Credenciales.pdf` → `faq_licencias`
  - `FAQ_Impacto_Regulatorio_y_Cambios_Operativos.pdf` → `faq_impactos_regulatorios`
  - `FAQ_Soporte_Tecnico_y_Acceso_a_Portales.pdf` → `faq_soporte_tecnico_acceso_portales`
  - `FAQ_Visibilidad_de_Clientes_y_Polizas_en_Plataforma.pdf` → `faq_visibilidad_clientes_polizas`
  - `FAQ_Gestion_de_Agentes_y_Estructura_de_Agencia.pdf` → `faq_gestion_agentes_estructura_agencia`

Se verificó que los 8 PDFs referenciados (los 7 nuevos + el renombrado) existen en
`pdf/` y que cada `category` usada en `use_cases.json` tiene su contraparte en
`DOCUMENTS`, para que `precalcular_embeddings.py` pueda indexarlos sin `⚠ Archivo no
encontrado`.

---

## 4. Mensaje de "escalar a un humano" duplicado en cada respuesta

`main.py` ya agrega, al final de **cualquier** respuesta RAG/SQL, una línea fija
hardcodeada: `'Recuerda que si tu consulta no fue efectiva, puedes escribir "escalar a
un humano"'` (prompts en las líneas ~1126-1128, ~1286-1289 y ~1452-1455). Sin embargo,
las 32 entradas de `ending_resolution` en `use_cases.json` (todas las preexistentes y
las 7 nuevas de la sección 1) **también** mencionaban "puedes escalar tu consulta a un
humano" con su propia redacción, así que el usuario veía la invitación a escalar
repetida dos veces al final de cada respuesta.

**Fix:** se eliminó la cláusula de escalamiento de los 32 `ending_resolution`,
conservando el resto de cada mensaje (invitación a reformular, links a módulos de ARC,
etc.). La opción de escalar queda cubierta únicamente por la línea fija del código.
Ejemplo (catálogo C, id 10):

- Antes: *"Si tienes otra duda sobre la gestión de agentes o la estructura de tu
  agencia, cuéntame más y te oriento. Si no queda resuelto, puedo escalar tu consulta a
  un humano."*
- Ahora: *"Si tienes otra duda sobre la gestión de agentes o la estructura de tu
  agencia, cuéntame más y te oriento."*

---

## Resumen de impacto

- Catálogo C: 10 → **11** casos de uso.
- Catálogo B: 15 → **14** casos de uso (se movió el 11 a C).
- Las 32 entradas de `use_cases.json` ya no repiten la invitación a "escalar a un
  humano" en su `ending_resolution` — esa opción se muestra una sola vez, vía la línea
  fija de `main.py`.
- 7 categorías FAQ nuevas + 1 renombrada (`faq_arc`) + 1 reubicada
  (`faq_reconciliaciones`), todas dentro de la colección única `documentos_normativos`.
- `scripts/precalcular_embeddings.py`: de 3 entradas FAQ dispersas (2 colecciones
  distintas) a 9 entradas consolidadas en `documentos_normativos`.
