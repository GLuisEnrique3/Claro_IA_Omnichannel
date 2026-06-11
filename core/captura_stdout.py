"""
Proxy de stdout por-contexto para capturar los print() del CLI sin tocar main.py.

Las funciones de ejecución de main.py (ejecutar_consulta, ejecutar_rag,
ejecutar_multiple, _mostrar_instrucciones, _header) imprimen directamente a
stdout. Para que Google Chat responda EXACTAMENTE lo mismo que el CLI, se
capturan esos prints tal cual, sin duplicar los textos en otro módulo.

A diferencia de contextlib.redirect_stdout (que swapea sys.stdout globalmente
y mezcla la salida entre requests concurrentes), este proxy enruta cada
write() según un ContextVar: si el contexto actual tiene un buffer activo,
escribe ahí; si no, escribe al stdout real. Los threads sin buffer (p. ej.
los workers de ejecutar_multiple, que no imprimen) no se ven afectados.

Uso:
    instalar_proxy()  # una sola vez, al arrancar la app

    with capturar() as buffer:
        main.ejecutar_rag(...)
    texto = buffer.getvalue()
"""
import contextvars
import io
import sys
from contextlib import contextmanager

_buffer_activo: contextvars.ContextVar[io.StringIO | None] = contextvars.ContextVar(
    "captura_stdout_buffer", default=None
)

_stdout_real = None


class _StdoutProxy:
    """Delega a un buffer del contexto actual o al stdout real."""

    def __init__(self, real):
        self._real = real

    def write(self, data: str) -> int:
        buf = _buffer_activo.get()
        if buf is not None:
            return buf.write(data)
        return self._real.write(data)

    def flush(self) -> None:
        buf = _buffer_activo.get()
        if buf is not None:
            return
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


def instalar_proxy() -> None:
    """
    Instala el proxy sobre sys.stdout. Idempotente.
    Si otro componente (p. ej. pytest) reemplazó sys.stdout después de una
    instalación previa, se re-instala envolviendo el stream actual.
    """
    global _stdout_real
    if isinstance(sys.stdout, _StdoutProxy):
        return
    _stdout_real = sys.stdout
    sys.stdout = _StdoutProxy(_stdout_real)


@contextmanager
def capturar():
    """Captura los print() del contexto actual en un StringIO."""
    instalar_proxy()  # auto-reparación si alguien swapeó sys.stdout
    buf = io.StringIO()
    token = _buffer_activo.set(buf)
    try:
        yield buf
    finally:
        _buffer_activo.reset(token)
