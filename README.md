# lisa-browser

Ejecutor de Browser Use de Lisa. Maneja un Chromium y deja que JEV, un modelo de decisión, elija cada acción sobre los elementos accesibles de la página. Requisitos y diseño: [SPEC](../../specs/browser-use/SPEC.md) y [PLAN](../../specs/browser-use/PLAN.md).

Estado: **en construcción**. Ver [TAREAS](../../specs/browser-use/TAREAS.md) para lo que está hecho y probado.

## Estructura

| Archivo | Contenido |
|---|---|
| `jev.py` | Transporte de arc-cua hacia JEV por la API de Command Code; cuenta llamadas, tokens y coste |
| `runner.py` | Ejecuta una subtarea con límites de pasos y tiempo y devuelve un estado estable, sin secretos |
| `session.py` | Abrir una página, leer su texto y capturarla |
| `journal.py` | Registro de operaciones en SQLite, sin secretos |
| `gate.py` | Barrera de aprobaciones: clasifica cada acción (enviar, borrar, dinero) y pide permiso antes de ejecutarla |
| `vault.py` | Baúl de contraseñas (Vaultwarden por `bw`) y coincidencia de dominios |
| `login.py` | Inicio de sesión con el baúl, un solo intento, y segundo factor |
| `files.py` | Adjuntar archivos, recoger descargas, listar y cerrar sesiones |
| `server.py` | API HTTP del ejecutor: operaciones con eventos, respuestas, cancelación, subidas, descargas y registro |
| `__main__.py` | Arranque con un archivo de configuración; ver `config.example.toml` |

## Pruebas

```bash
nix-shell --run "python -m unittest discover -s tests -t . -v"
```

`shell.nix` trae Python con arc-cua y Chromium. Las pruebas no usan JEV ni internet: sirven su propio sitio web, arrancan Chromium sin interfaz y sustituyen al modelo por una política fijada.
