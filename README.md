# lisa-browser-use

El navegador de Lisa. Un servicio que maneja un Chromium propio y hace en él lo que le pide el agente de Pablo, sea cual sea (se conecta como MCP, ver [docs/MCP.md](docs/MCP.md)): abrir y leer páginas, dar pasos en ellas, iniciar sesión con el baúl de contraseñas, descargar y adjuntar archivos, y repetir gestiones que ya sabe hacer.

Trabajan dos modelos, y casi todo el coste está en el primero:

| Quién | Qué decide | Coste |
|---|---|---|
| El planificador (el agente que use Pablo, conectado por el MCP único; hasta el 17-10-2026, [lisa-agent](https://github.com/PabloPC05/lisa-agent)) | Qué página abrir y qué paso pedir | El del modelo grande, en cada llamada |
| JEV, dentro de este servicio | Cada clic y cada tecla de un paso, sobre los elementos accesibles de la página, sin capturas | 0,042 $ por millón de tokens de entrada |

Por eso todo lo que este servicio hace por sí solo, sin devolver el control al planificador, abarata la tarea.

Y lo que no necesita ningún modelo, mejor todavía: seguir un enlace por su nombre (`follow`) lo resuelve el servicio mirando la página, sin JEV, y sin equivocarse de enlace.

El paquete de Python sigue llamándose `lisa_browser`: es el nombre que usa el servicio desplegado.

## Documentos

| Documento | Contenido |
|---|---|
| [docs/MCP.md](docs/MCP.md) | El navegador como MCP: herramientas, formularios a Pablo y límites (desde el 17-10-2026) |
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
| `certificate.py` | Certificado digital del baúl (`.p12`) para sedes electrónicas, presentado por el proxy de `sidecar/` |
| `card.py` | Pago con tarjeta que Pablo da en el momento (tasas de trámites), con aprobación de dinero |
| `files.py` | Adjuntar archivos, recoger descargas, listar y cerrar sesiones |
| `library.py` | Biblioteca: sitios habituales y rutinas guardadas |
| `journal.py` | Registro de operaciones en SQLite, sin secretos |
| `mcp_server.py` | El MCP del navegador (destino `browser` del MCP único): herramientas, formularios a Pablo y resultados |
| `tramites.py` | Sedes oficiales y búsqueda en el BOE |
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

## Trámites: certificado digital y tasas

Para hacer trámites en sedes electrónicas (propuesta en `lisa-assistant/docs/tramites.md`, LISA-70 a LISA-77).

- **Certificado.** Chromium no puede elegir un certificado de cliente sin pantalla. Lo presenta un proxy en Go, `sidecar/`, tomado de [SpainAssist](https://github.com/MeLlamoPablo/SpainAssist) (MIT, ver `sidecar/NOTICE`):
  - Chromium solo manda por él los dominios oficiales de la configuración, que decide su PAC.
  - Con `login` y `method: "certificate"`, el ejecutor saca del baúl el adjunto `.p12`/`.pfx` y su contraseña (un campo «Contraseña» de la entrada, o la contraseña de la entrada). Durante 30 minutos el proxy presenta el certificado a esas sedes; aguanta la renegociación TLS 1.2 que usan algunas.
  - La clave solo vive en memoria del ejecutor y del proxy.
  - El proxy solo atiende al contenedor del navegador y nunca conecta con direcciones privadas, de loopback ni de Tailscale (`sidecar/lisa.go`).
- **Tasas.** La operación `card` pide la tarjeta a Pablo como dato secreto (evento `secret` con `kind: "card"`), la rellena y pulsa pagar.
  - JEV ve `[secret]` en el número y el CVV, y el registro los oculta.
  - La aprobación de dinero muestra solo las cuatro últimas cifras, y las capturas ocultan los campos de tarjeta.

`tests/test_sede.py` monta una sede HTTPS que pide el certificado de cliente y la alcanza con el binario real del proxy, configurado como el ejecutor. Prueba que sin certificado la sede rechaza (403), con él entra, y que un dominio fuera de la lista se rechaza. El proxy nunca conecta con direcciones internas; esa sede de prueba vive en loopback y se permite con `allow_private`/`allowPrivateHosts`, que solo usan las pruebas y queda vacío en producción.

## Navegador en vivo

`GET /v1/screencast`, con el token, emite la pestaña en la que trabaja el ejecutor mientras alguien mira (lisa-assistant D86). Devuelve una línea JSON por evento: `frame` (JPEG en base64, con ancho y alto), `idle` (aún no hay navegador) y `ping` cada 15 s. Va por una conexión propia a Chrome (`Page.startScreencast`), así que no espera a que termine una tarea ni la retrasa. Hay una sola emisión para todos los que miran, a 5 fotogramas por segundo como mucho, y se para cuando se va el último. Los fotogramas no se guardan. Solo sirve para mirar.

## Pruebas

```bash
nix-shell --run "python -m unittest discover -s tests -t . -v"
```

`shell.nix` trae Python con arc-cua, Chromium y el proxy de certificados compilado (`LISA_SIDECAR`). Las pruebas del proxy en sí: `cd sidecar && go test ./...`. Las pruebas no usan JEV ni internet: sirven su propio sitio web, arrancan Chromium sin interfaz y sustituyen al modelo por una política fijada. Si el entorno no permite el aislamiento de procesos de Chromium, `LISA_TEST_CHROME_ARGS="--no-sandbox"`.

## Despliegue

Corre en el servidor de Lisa como el servicio `lisa-browser`, junto a un Chromium en un contenedor; lo define `hosts/lisa/navegador.nix` del repositorio [lisa](https://github.com/PabloPC05/lisa). Solo escucha en `127.0.0.1:8787` (la API) y `127.0.0.1:8789` (el MCP), y cada petición lleva un token.

Desde el 12-10-2026 el servicio ejecuta este repositorio, clonado en `/srv/lisa/lisa-browser-use` (desde el 17-10-2026, del repositorio público por HTTPS, sin clave). Actualizarlo:

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
