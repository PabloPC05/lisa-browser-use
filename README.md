# lisa-browser-use

El navegador de Lisa. Un servicio que maneja un Chromium propio y hace en él lo que Lisa le pide: abrir y leer páginas, dar pasos en ellas, iniciar sesión con el baúl de contraseñas, descargar y adjuntar archivos, y repetir gestiones que ya sabe hacer.

Trabajan dos modelos, y casi todo el coste está en el primero:

| Quién | Qué decide | Coste |
|---|---|---|
| El planificador (Claude, en [lisa-agent](https://github.com/PabloPC05/lisa-agent) o en el puente de Telegram) | Qué página abrir y qué paso pedir | El del modelo grande, en cada llamada |
| JEV, dentro de este servicio | Cada clic y cada tecla de un paso, sobre los elementos accesibles de la página, sin capturas | 0,042 $ por millón de tokens de entrada |

Por eso todo lo que este servicio hace por sí solo, sin devolver el control al planificador, abarata la tarea.

Y lo que no necesita ningún modelo, mejor todavía: seguir un enlace por su nombre (`follow`) lo resuelve el servicio mirando la página, sin JEV, y sin equivocarse de enlace.

El paquete de Python sigue llamándose `lisa_browser`: es el nombre que usa el servicio desplegado.

## Documentos

| Documento | Contenido |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | Requisitos R1–R13 y decisiones |
| [docs/PLAN.md](docs/PLAN.md) | Arquitectura, contratos de la API, barrera de aprobaciones e inicio de sesión |
| [docs/TAREAS.md](docs/TAREAS.md) | Qué está hecho, con su evidencia, y qué falta |

## Estructura

| Archivo | Contenido |
|---|---|
| `jev.py` | Transporte de arc-cua hacia JEV por la API de Command Code; cuenta llamadas, tokens y coste |
| `runner.py` | Ejecuta una subtarea con límites de pasos y tiempo y devuelve un estado estable, sin secretos |
| `session.py` | Abrir una página, leer su texto y capturarla |
| `gate.py` | Barrera de aprobaciones: clasifica cada acción (enviar, borrar, dinero) y pide permiso antes de ejecutarla |
| `vault.py` | Baúl de contraseñas (Vaultwarden por `bw`) y coincidencia de dominios |
| `login.py` | Inicio de sesión con el baúl, un solo intento, y segundo factor |
| `files.py` | Adjuntar archivos, recoger descargas, listar y cerrar sesiones |
| `library.py` | Biblioteca: sitios habituales y rutinas guardadas |
| `journal.py` | Registro de operaciones en SQLite, sin secretos |
| `server.py` | API HTTP: operaciones con eventos, respuestas, cancelación, subidas, descargas, registro y biblioteca |
| `__main__.py` | Arranque con un archivo de configuración; ver `config.example.toml` |

## La biblioteca: sitios y rutinas

Sin memoria, cada gestión se redescubre: el planificador tantea direcciones y decide paso a paso. Medido el 12-10-2026, localizar una página en una web que no conocía le costó entre 13 y 19 llamadas al modelo grande.

- Un **sitio** guarda la dirección de entrada y una nota de dónde está cada cosa.
- Una **rutina** guarda los pasos que funcionaron para una gestión que se repite. El servicio los hace seguidos y el planificador recibe solo el final.

```jsonc
// PUT /v1/library/routines/Notas del campus
{
  "description": "Dice las notas de una asignatura en el campus virtual.",
  "steps": [
    { "op": "open", "url": "https://cv.usc.es/my/" },
    { "op": "login" },
    { "op": "task", "goal": "Abrir la asignatura {asignatura}", "verification": ["Se ve la página de {asignatura}"] },
    { "op": "task", "goal": "Abrir las calificaciones", "verification": ["Se ve la tabla de calificaciones"], "find": "nota, total" }
  ]
}
// POST /v1/operations  { "op": "routine", "args": { "name": "notas del campus", "params": { "asignatura": "Álgebra" } } }
```

Una rutina guarda el objetivo de cada paso, no los clics: JEV vuelve a decidir sobre la página de ese momento, así que aguanta cambios pequeños de la web. Si un paso no sale, se detiene y dice cuál. No guarda contraseñas (el paso `login` usa el baúl y se salta si la sesión sigue abierta) y no rodea la barrera: enviar, borrar o pagar siguen pidiendo permiso. Detalle en [PLAN §12](docs/PLAN.md).

## Pruebas

```bash
nix-shell --run "python -m unittest discover -s tests -t . -v"
```

`shell.nix` trae Python con arc-cua y Chromium. Las pruebas no usan JEV ni internet: sirven su propio sitio web, arrancan Chromium sin interfaz y sustituyen al modelo por una política fijada. Si el entorno no permite el aislamiento de procesos de Chromium, `LISA_TEST_CHROME_ARGS="--no-sandbox"`.

## Despliegue

Corre en el servidor de Lisa como el servicio `lisa-browser`, junto a un Chromium en un contenedor; lo define `hosts/lisa/navegador.nix` del repositorio [lisa](https://github.com/PabloPC05/lisa). Solo escucha en `127.0.0.1:8787` y cada petición lleva un token.

Desde el 12-10-2026 el servicio ejecuta este repositorio, clonado en `/srv/lisa/lisa-browser-use` con una clave de solo lectura (`~/.ssh/github_lisa_browser_use_deploy`). Actualizarlo:

```bash
git -C /srv/lisa/lisa-browser-use pull && sudo systemctl restart lisa-browser
```

La biblioteca está en `/var/lib/lisa-browser/biblioteca.sqlite`, junto al registro.

## Medido con una gestión real

«Descarga del campus virtual el PDF del enunciado de la práctica 2 de Sistemas Operativos I», con Claude Sonnet 5.5 como planificador (12-10-2026):

| Lo que hay guardado | Cómo se siguen los enlaces | Llamadas del planificador | Pasos de JEV | Tiempo | Tokens del planificador | Resultado |
|---|---|---|---|---|---|---|
| Nada | JEV | 20 | 0 | 242 s | 341.000 | No llegó a encontrar el campus |
| El sitio | JEV | 15 | 10 | 73 s | 260.000 | PDF entregado (y antes, uno equivocado) |
| El sitio | `follow` | 5 | 0 | 19 s | 74.000 | PDF entregado |
| El sitio y la rutina | JEV | 1 | 5 | 14 s | 26.000 | PDF entregado |
| El sitio y la rutina | `follow` | 1 | 0 | 6–11 s | 27.000 | PDF entregado |

Son pocas pasadas (una o dos por fila; la última, tres). La fila de `follow` sin rutina es otro documento del mismo curso, pedido paso a paso. Lo que gasta JEV es aparte y casi no cuenta: 59.000 tokens suyos en la segunda fila son 0,0025 $.

Casi todos los tokens del planificador son relectura de lo ya enviado, que el proveedor cobra a una décima parte: ponderados por precio, la segunda fila son unos 50.000 y la última unos 4.300. El suelo actual son dos llamadas (decidir y contestar) con unos 13.500 tokens fijos cada una, entre instrucciones, índice de memoria y herramientas.

Lo que enseñó esta gestión:

- La rutina que guardó el planificador usaba un paso de JEV para pulsar el enlace, y JEV pulsó a veces el de otra práctica. Con `follow` no hay nada que decidir: o existe un enlace con ese nombre o la rutina se detiene y lo dice (así terminó la petición de una práctica 3 que todavía no existe).
- En una pasada la rutina falló por un error del navegador justo después de reiniciar el servicio; el planificador terminó a mano desde donde quedó (3 llamadas, 19 s).

## Origen

El código viene de `packages/lisa-browser` de [lisa-project](https://github.com/PabloPC05/lisa-project) (rama `browser-use`), con su historial. Los documentos de `docs/` estaban allí en `specs/browser-use/`.
