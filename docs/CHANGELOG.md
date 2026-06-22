# Changelog — `main_old.py` → `main.py`

Este documento detalla los cambios entre la versión anterior del núcleo de la
aplicación (`main_old.py`) y la versión actual (`main.py`). El cambio central
es el reemplazo del motor de detección de intención (de similitud coseno por
embeddings a dos agentes LLM conversacionales), con varios cambios derivados
en el manejo de historial, confirmación y cierre de sesión.

---

## 1. Motor de detección de intención — rediseño completo

### Antes
Pipeline de dos pasos, ambos con un componente de matching por embeddings:

1. `transformar_consulta_con_llm()` — un LLM normalizaba la consulta del
   usuario, eliminando entidades específicas (carriers, estados, fechas,
   nombres) y dejando solo la intención ("cuántos contratos activos con
   Humana en Florida" → "cuántos contratos activos").
2. `detectar_caso_de_uso()` — esa consulta normalizada se comparaba por
   similitud coseno contra embeddings **precalculados** de cada caso de uso
   (`data/use_cases_embeddings.pkl`), filtrando primero por catálogos
   permitidos. Solo se aceptaba un match si superaba `_UMBRAL_INTENT = 0.85`.
3. Si no se superaba el umbral, el flujo terminaba ahí: se le decía al
   usuario "no fue posible identificar un flujo asociado" y se le pedía
   reformular, sin más contexto.
4. La confirmación posterior era estrictamente S/N (`_input_sn`).

**Dependencias que esto traía:** `import pickle`, carga de
`_USE_CASES_EMB_PATH` / `_USE_CASES_EMBEDDINGS`, `CATALOG_LABELS` (para el
menú manual), y el script `scripts/precalcular_use_cases.py` debía
ejecutarse cada vez que se agregaba o cambiaba un caso de uso.

### Ahora
Pipeline de dos agentes LLM, sin embeddings ni umbral numérico fijo:

1. **Agente 1** — `seleccionar_caso_de_uso_llm()`: el LLM recibe la lista
   completa de casos de uso permitidos junto con su descripción
   `usa_esto_cuando` (texto libre en `use_cases.json`) y decide directamente
   cuál aplica a la consulta, sin pasar por una etapa de normalización
   previa. Si la consulta es conversacional/meta sobre el propio asistente
   ("quién eres", "qué puedes hacer", "qué temas puedo consultar"), el LLM lo
   detecta y responde directo usando un recurso de presentación construido
   dinámicamente (`_construir_recurso_presentacion()` + `_TEMAS_POR_CATALOGO`),
   sin entrar al flujo de negocio.
2. **Agente 2** — `_interpretar_confirmacion()`: el LLM interpreta la
   respuesta libre del usuario al mensaje de confirmación del Agente 1
   (confirmó / corrigió con una aclaración / rechazó sin más). Si no
   confirma pero la respuesta trae información útil, se genera una
   `query_ajustada` que se vuelve a pasar al Agente 1. **No hay límite de
   reintentos**: el loop sigue hasta que el usuario confirme explícitamente o
   use un comando global (`salir`, `volver`, `nueva sesión`).
3. Ya no existe una etapa de "no se encontró nada, reformule" con salida
   abrupta — el ciclo de afinamiento entre Agente 1 y Agente 2 reemplaza esa
   rama de error.

**Lo que esto elimina:**
- `transformar_consulta_con_llm()` y `_PROMPT_NORMALIZAR_INTENT` (prompt
  grande con ~25 ejemplos de normalización).
- `detectar_caso_de_uso()` y `_UMBRAL_INTENT`.
- Carga de `use_cases_embeddings.pkl` y el `import pickle`.
- Necesidad de re-ejecutar `precalcular_use_cases.py` al agregar casos de
  uso nuevos — ahora basta con escribir bien el campo `usa_esto_cuando`.

---

## 2. Confirmación del usuario — de S/N a lenguaje libre

| | Antes | Ahora |
|---|---|---|
| Mecanismo | `_input_sn()` — solo acepta `S`, `N` o Enter | Texto libre interpretado por el Agente 2 |
| Corrección | `N` → se descarta todo y se reinicia desde el `VoverError` | El LLM extrae una `query_ajustada` y reclasifica sin perder el hilo |
| Reintentos | Implícito (1 intento de clasificación, luego reformular manualmente) | Sin límite — itera hasta confirmar o comando global |
| Logging | `q.caso_score` (score de similitud) | `q.confirmacion_mensaje`, `q.confirmado`, `q.intentos_confirmacion` |

---

## 3. Preguntas conversacionales / meta sobre el asistente (nuevo)

No existía en la versión anterior. Ahora, si el usuario pregunta algo como
"¿qué eres?" o "¿qué temas puedo consultar?", el Agente 1 lo detecta
(`es_meta_conversacional: true` en su respuesta JSON) y contesta usando:

- `_TEMAS_POR_CATALOGO` — texto de los temas disponibles por catálogo (A/B/C).
- `_construir_recurso_presentacion()` — ensambla esos temas según los
  catálogos permitidos del usuario actual y se lo pasa al LLM como fuente de
  verdad, para que no invente una respuesta genérica.

Este turno se registra en el historial conversacional pero no pasa por
extracción de entidades, SQL ni RAG.

---

## 4. Historial conversacional — ahora incluye la respuesta real del asistente

### Antes
`_historial_reciente` solo guardaba la consulta del usuario:
```python
_historial_reciente.append({"role": "user", "content": user_query_efectiva})
```
La reescritura de contexto (`reescribir_consulta`) solo tenía acceso a lo que
el usuario había preguntado antes, no a lo que el sistema había respondido.

### Ahora
Se guarda también la respuesta del asistente:
```python
_historial_reciente.append({"role": "user", "content": user_query_efectiva})
_historial_reciente.append({"role": "assistant", "content": (respuesta_mostrada or "")[:300]})
```
Esto requirió cambiar la firma de dos funciones para que devuelvan el texto
de la respuesta en vez de solo imprimirlo:

| Función | Antes | Ahora |
|---|---|---|
| `ejecutar_rag()` | `-> None` (solo `print`) | `-> str` (imprime y retorna el texto) |
| `ejecutar_multiple()` | `-> None` (solo `print`) | `-> str` (imprime y retorna el texto sintetizado) |

---

## 5. Reescritura de consulta (`reescribir_consulta`) — ventana de contexto

| | Antes | Ahora |
|---|---|---|
| Mensajes de contexto usados | Solo el último (`_historial_reciente[-1:]`) | Los últimos dos (`_historial_reciente[-2:]`) |
| Atajo por longitud | Si la consulta tenía más de 4 palabras, se saltaba el rewrite por completo (`if len(user_query.split()) > 4`) | Sin atajo — siempre se evalúa, y la Regla 2 del prompt (consulta completa por sí sola) ya cubre ese caso desde el propio LLM |

---

## 6. Cierre de sesión y encadenado de la siguiente consulta

### Antes
```python
continuar = _input_sn("¿Desea realizar otra consulta? (S/N): ")
if continuar != "S":
    ...
    break
```
Estrictamente S/N; si el usuario quería seguir, debía escribir "S" y luego,
en la siguiente vuelta del loop, recién escribir su consulta.

### Ahora
```python
siguiente = _input("¿Hay algo más en lo que pueda ayudarte? ").strip()
if not siguiente or siguiente.lower() in _PALABRAS_NO:
    ...
    break
_next_query = siguiente
```
Se agregó el set `_PALABRAS_NO = {"no", "n", "no gracias", "nada", "nada mas",
"nada más", "no, gracias", "eso es todo", "ninguna"}`. Si el usuario escribe
directamente su siguiente pregunta en esa misma respuesta, se encadena vía
`_next_query` sin pasar de nuevo por el prompt "¿Qué desea consultar hoy?".

---

## 7. `_mostrar_instrucciones()` — contenido recortado

Se eliminaron dos bloques estáticos grandes:
- **"FILTROS QUE RECONOCE EL SISTEMA"** (tabla de ejemplos por tipo de filtro).
- **"EJEMPLOS DE CONSULTAS COMPLETAS"** (lista de ~19 consultas de ejemplo).

Quedan: flujo general, temas que puede consultar (resumen estático) y
comandos globales. La razón es que esa información ahora puede pedirse en
lenguaje natural y el Agente 1 la responde dinámicamente usando el recurso de
presentación (punto 3), por lo que mantener un bloque estático duplicado dejó
de ser necesario para cubrir esos casos.

---

## 8. Funciones y datos eliminados (sin reemplazo directo)

| Elemento | Motivo |
|---|---|
| `seleccionar_catalogo()` | Menú manual de catálogo — no estaba siendo invocado por `ciclo_consultas`, quedó como código sin uso. |
| `seleccionar_pregunta()` | Mismo caso — menú manual de pregunta dentro de un catálogo, sin uso. |
| `CATALOG_LABELS` | Solo lo usaban las dos funciones de menú manual anteriores. |
| `_USE_CASES_EMB_PATH`, `_USE_CASES_EMBEDDINGS`, `_UMBRAL_INTENT` | Ya no hay matching por embeddings de casos de uso (ver punto 1). |
| `import pickle` | Sin otro uso en el archivo una vez quitada la carga de embeddings de casos de uso. |

---

## 9. `config/logger.py` — tabla de BigQuery y nuevos campos de logging

Este archivo no es parte de `main.py` pero cambió en conjunto, porque
`QueryLog` registra los campos nuevos que introdujo el rediseño de
confirmación (punto 2).

| | Antes | Ahora |
|---|---|---|
| Tabla destino en BigQuery | `claroinsurance-dataplatform.claro_IA.model_tracking` | `claroinsurance-dataplatform.claro_IA.model_tracking_poc` (`_BQ_TABLE` en `config/logger.py:11`) |
| Campo `caso_score` | Presente (score de similitud coseno del caso de uso detectado) | Eliminado — ya no existe un score numérico, el Agente 1 decide directamente |
| Campos nuevos | — | `confirmacion_mensaje`, `confirmado`, `intentos_confirmacion` (ver punto 2) |
| Error al insertar en BigQuery (`_flush_bq`) | `except Exception: pass` — se silenciaba sin dejar rastro | `except Exception as exc: print(f"⚠ No se pudo registrar la consulta en BigQuery ({_BQ_TABLE}): {exc}")` — el error queda visible en consola |

> **Nota:** al momento de escribir este changelog, el cambio de tabla a
> `model_tracking_poc` está hecho en el código pero **no commiteado**
> (`git status` lo marca como `M config/logger.py`). Si la tabla
> `model_tracking_poc` todavía no existe en BigQuery con el esquema de
> `QueryLog.to_dict()`, el insert va a fallar — ahora ese fallo se imprime en
> consola en vez de pasar desapercibido.

---

## 10. Cambios menores

- `_buscar_semantico()` y `extraer_entidades()`: `torch.tensor(embs_pre, device=...)`
  se simplificó a `torch.tensor(embs_pre)` (sin fijar `device` explícitamente
  al crear el tensor de embeddings precalculados).
- `_detectar_carrier_rag()` en `ejecutar_rag()`: el print de confianza del
  carrier detectado (`Carrier detectado: {carrier_match} (confianza: ...)`)
  quedó comentado.
- Se quitaron los `print` de debug (`#--DEBUG Consulta reescrita: ...`,
  `#--DEBUG Pregunta Formateada: ...`) que exponían pasos internos del
  pipeline en la consola.

---

## 11. Lo que se mantuvo igual

Para que quede claro qué **no** cambió entre ambas versiones:

- Identificación de usuario (`identificar_usuario()`, `TIPOS_USUARIO`,
  umbrales por tipo) — sin cambios.
- `PARAM_TO_FILTRO` y toda la extracción de entidades (`extraer_entidades()`),
  incluyendo las ramas especiales de fecha, commission month, ID de
  oportunidad, número de póliza y NPN — sin cambios funcionales.
- `_parse_fecha_natural()` — idéntico.
- Construcción de SQL con LLM (`_construir_sql_con_llm()`), reintentos ante
  error de BigQuery, y la capa de seguridad que bloquea SQL de escritura
  (`_SQL_FORBIDDEN`) — sin cambios.
- Ejecución RAG sobre ChromaDB (`_aplicar_filtro_agencia()`, detección de
  carrier por n-gramas) — misma lógica, solo cambia el tipo de retorno (ver
  punto 4).
- Ejecución de sub-casos en paralelo (`ejecutar_multiple()`,
  `_ejecutar_consulta_silenciosa()`, `_ejecutar_rag_silenciosa()`,
  `_sintetizar_respuestas_multiples()`) — misma lógica.
- Sistema de logging (`config/logger.py`, campos de `query_log`) — se le
  agregaron campos nuevos relacionados a la confirmación (punto 2), pero los
  campos existentes no cambiaron.
- `main()` — estructura idéntica (identificación → resolución de agencia →
  bienvenida → `ciclo_consultas()`).

---

## Resumen ejecutivo

La versión nueva reemplaza un pipeline rígido de clasificación por similitud
coseno + confirmación S/N por un pipeline de dos agentes LLM que clasifican y
confirman en lenguaje natural, soportan preguntas meta-conversacionales sobre
el propio asistente, e iteran sobre correcciones del usuario sin reiniciar el
flujo desde cero. Como consecuencia, ya no se depende de
`use_cases_embeddings.pkl` ni de `precalcular_use_cases.py`, y el historial
conversacional ahora incluye las respuestas reales del asistente (no solo las
preguntas del usuario), lo que mejora la calidad de la reescritura de
consultas de seguimiento.
