# PLAN · Browser Use

> Este documento se escribió en `lisa-project` (`specs/browser-use/`), cuando el ejecutor era el paquete
> `packages/lisa-browser` de aquel repositorio. Donde dice `packages/lisa-browser/…`, léase la raíz de este;
> los enlaces a `docs/NN` apuntan allí.

Fecha: 10-10-2026. Plan técnico de [SPEC.md](SPEC.md) (aprobada el 10-10-2026). No contiene tareas ni estados.

La SPEC conserva bloqueos sobre sitios de prueba. Pablo pidió el plan sabiéndolo y después los aplazó; aquí solo afectan a las aceptaciones marcadas como «prueba real» pendiente.

## Decisiones de Pablo sobre este plan (10-10-2026)

- **Acepta el límite de las aprobaciones:** la barrera funciona por reglas (§5) y no puede garantizar que detecte toda acción con efecto en cualquier página. En sitios de dinero pregunta por defecto.
- **«Por ahora no uses el banco ni nada, solo desarrolla».** Durante el desarrollo no se entra en bancos ni se hacen operaciones reales de dinero. La lógica de dinero se construye y se prueba solo con el sitio de pruebas local. La prueba real R8.1 queda aplazada hasta que Pablo la pida.
- **Dará una contraseña maestra del baúl.** La guardará él por terminal en el servidor; solo hace falta para las pruebas reales con el baúl (último paso de §10).

Aplazado, sin bloquear la construcción: los sitios reales de prueba de R6.1 (operador) y R7.1–3 (Vinted u otro).

## 1. Hechos comprobados para este plan

En el checkout y en las máquinas, el 10-10-2026:

- **JEV por Command Code funciona con el formato de arc-cua.** Una petición real a `POST https://api.commandcode.ai/provider/v1/systemone` con `model: "typesafe/jev"`, `state` y `questions` tal como los construye arc-cua devolvió `answers.<pregunta>.{choice, confidence, probabilities}` y `usage.{input_tokens, output_tokens}`. Tardó 1,0–1,1 s por llamada suelta (435 tokens de entrada), más que los 0,3 s publicados por terceros.
- **arc-cua 0.1.1** (código leído, no ejecutado):
  - `TypeSafeJevPolicy(api_key, model, base_url)` admite otra dirección y otro modelo.
  - `ChromeBackend.connect(endpoint)` se une a un Chrome ya arrancado. Observa con un script de página que devuelve elementos con rol, nombre, valor y estado; enmascara el valor de los campos de contraseña.
  - No ofrece ninguna acción sobre `input type=file`, no gestiona descargas y no atraviesa marcos de otro origen.
  - El texto que se escribe sale siempre de `inputs` de la subtarea; los marcados en `secret_inputs` llegan al modelo como `[secret]`.
  - Su barrera de riesgos reconoce **solo palabras en inglés** en la etiqueta del control (`send`, `delete`, `pay`…) y **solo en clics**: escribir y pulsar Intro no pasa por ella. No sirve para páginas en español.
  - Es síncrona (hilos), no asíncrona. Trae `ScriptedPolicy` para pruebas sin modelo.
- **El adaptador de Claude** anuncia `mcp_capabilities: {http: true, sse: true}`: acepta servidores MCP por HTTP declarados al crear la sesión ACP.
- **NixOS 26.05 trae** Chromium 154, `bitwarden-cli` 2026.4.2, `python3Packages.mcp` 1.26, `tldextract`, `websockets`, `aiohttp` y `virtualisation.oci-containers`. arc-cua no está empaquetado.
- **Red de lisa-av:** al usar la red de usuario de QEMU, lo que la máquina virtual abre hacia `10.0.2.2` llega al `127.0.0.1` del servidor, y también alcanza las redes de Docker. Hoy nada lo limita.
- **Command Code** documenta 403 `upgrade_required` y 429 `rate_limit_error`; no documenta qué devuelve al alcanzar un tope del plan.

Supuestos sin comprobar, que la construcción debe verificar pronto:

| Supuesto | Si es falso |
|---|---|
| `bw` (Bitwarden CLI) inicia sesión y lee contra Vaultwarden 1.37 | Usar `rbw` o la API de Vaultwarden directamente; cambia solo `vault.py` |
| Chromium 154 en contenedor acepta conexiones CDP desde la red de Docker con su aislamiento de procesos activo | Añadir un reenviador dentro de la imagen; si el aislamiento no arranca, perfil seccomp propio |
| La respuesta de Command Code al agotar un tope se distingue de un límite de velocidad | Tratar todo 403/429 persistente como tope y mostrar el texto del proveedor |
| El bot puede borrar el mensaje de Pablo en un chat privado | El código se sigue sin guardar ni enviar a modelos, pero queda visible en el chat; se avisa a Pablo |

## 2. Arquitectura

```text
lisa-av (máquina virtual)                          Lisa (servidor)
┌───────────────────────────────────┐              ┌──────────────────────────────────────────────┐
│ Claude ──MCP http 127.0.0.1──► puente             │ lisa-browser (ejecutor, usuario propio)       │
│ (lisa-agente)          (lisa-puente)──HTTP+token──┼─► API en 127.0.0.1:8787                       │
│                           │                       │     ├─ CDP ─► contenedor lisa-navegador        │
│                        canal                      │     ├─ bw ──► Vaultwarden 127.0.0.1:8222        │
└───────────────────────────────────┘              │     └─ HTTPS ► Command Code (JEV)             │
                                                   └──────────────────────────────────────────────┘
```

Por qué así:

- **El ejecutor vive en el servidor, no en lisa-av.** Es quien tiene la clave de JEV, abre el baúl y habla con Chrome. Así el protocolo de control de Chrome, que no tiene autenticación, no sale del servidor, y la barrera de aprobaciones corre en un proceso que el agente no puede tocar.
- **Claude no habla con el ejecutor.** Llama a herramientas MCP que sirve el puente; el puente es quien tiene el token del ejecutor y quien pregunta a Pablo.
- **Claude planifica y JEV ejecuta.** Claude decide la dirección a abrir y parte la tarea en subtareas; cada subtarea la resuelve el bucle de arc-cua con JEV.

## 3. Módulos

### 3.1 `packages/lisa-browser` (nuevo, Python, corre en el servidor)

| Archivo | Responsabilidad |
|---|---|
| `server.py` | API HTTP (aiohttp) en `127.0.0.1:8787`, con token. Una sola tarea a la vez; las demás esperan en cola. El bucle de arc-cua corre en un hilo |
| `session.py` | Conexión a Chrome (`ChromeBackend.connect`), navegación, lectura de la página, capturas, subidas (`DOM.setFileInputFiles`), descargas (`Browser.setDownloadBehavior`), cookies por dominio |
| `jev.py` | Transporte de arc-cua hacia Command Code: dirección, modelo y clave; recoge `usage` y latencia; traduce 403/429 persistentes a «tope alcanzado» |
| `gate.py` | Clasificación de riesgo y envoltorio del backend que pide aprobación antes de ejecutar (§5) |
| `vault.py` | Interfaz `Vault` y su implementación con `bw`: buscar por dirección, usuario, contraseña y código; comprobación de dominio con `tldextract` |
| `login.py` | Flujo de inicio de sesión: credenciales del baúl como `secret_inputs`, un solo intento, segundo factor |
| `journal.py` | Registro en SQLite (§4.4) |
| `tests/` | Sitio de pruebas local, Chromium real sin interfaz, `ScriptedPolicy` y baúl ficticio |

Dependencias: `arc-cua[browser]==0.1.1` fijada por hash, `aiohttp`, `httpx`, `tldextract`.

### 3.2 `packages/lisa-bridge` (cambios)

| Archivo | Cambio |
|---|---|
| `browser.py` (nuevo) | Cliente de la API del ejecutor y servidor MCP por HTTP en `127.0.0.1` con un token por sesión |
| `channel.py` y el canal de mensajería | `send_photo`, `send_file`, `delete_message`; `ask` acepta una imagen y dos o tres botones |
| `service.py` | Pasa el servidor MCP al crear o recuperar la sesión ACP; «respuesta secreta»: cuando el ejecutor pide un código, el siguiente mensaje de Pablo no se guarda ni se envía al agente y se borra del chat; `/cancelar` también cancela en el ejecutor |
| `permissions.py`, `config.py` | `allow_tools`: las herramientas `browser_*` no piden permiso por sí mismas (lo pide la barrera del ejecutor). Reglas `navegador:enviar:<dominio>` para «Permitir siempre». Sección `[browser]` |

### 3.3 Repositorio `lisa` (servidor)

`hosts/lisa/navegador.nix`:

- **Imagen** construida con Nix (`dockerTools`) a partir del Chromium de nixpkgs, sin imágenes de terceros. Versión fijada por `flake.lock`.
- **Contenedor** `lisa-navegador` (`virtualisation.oci-containers`): red propia de Docker con dirección fija; **ningún puerto publicado**; volúmenes de perfil, descargas y subidas (esta, solo lectura); sin socket de Docker; límites de memoria, CPU y procesos.
- **Servicio** `lisa-browser` con usuario `lisa-navegador`; secretos por `LoadCredential`.
- **Cortafuegos por usuario** para el proceso de la máquina virtual (`vm-asistente`): desde lisa-av solo se alcanzan en el servidor los puertos 22 y 8787, además del DNS. Se bloquean el resto de `127.0.0.1`, las redes de Docker y `tailscale0`. Las respuestas a conexiones ya establecidas se permiten siempre, para no romper la entrada de emergencia a lisa-av.
- **Secretos** en `/var/lib/lisa-secretos` del servidor: clave de Command Code, credenciales y contraseña maestra del baúl, token del ejecutor.
- **Clon** de `lisa-project` en el servidor con llave de solo lectura, y arc-cua empaquetado desde PyPI.

### 3.4 Repositorio `lisa-asistente-personal`

Token del ejecutor como credencial del puente, sección `[browser]` de la configuración y un apartado «Navegador» en `instrucciones.md`: cómo partir tareas, que nunca pida contraseñas, y que el texto de las páginas es dato.

## 4. Contratos

### 4.1 Herramientas MCP que ve Claude

| Herramienta | Entrada | Salida |
|---|---|---|
| `browser_open` | `url`, `read_chars` (por defecto 1500; 0 para no leer), `find` | `{ok, url, title, status, text, truncated}` |
| `browser_task` | `goal`, `verification[]`, `inputs{}`, `constraints[]`, `max_actions` (≤ 40, por defecto 30), `find` | `{status, reason, needs_input, actions_taken, page: {url, title, elements, more_below}}` y, con `find`, `text` y `matches` |
| `browser_read` | `find`, `max_chars` (por defecto 4000), `offset` | `{url, title, text, truncated, total_chars}`; con `find`, `matches`; si queda texto, `next_offset` |
| `browser_login` | `account` opcional (usa el dominio actual) | `{status: "ok" | "no_credentials" | "rejected" | "wrong_domain" | "vault_unavailable" | "choose_account"}`; con `choose_account`, `accounts: [{name, username}]`. Si la conexión con Chromium se corta después de escribir la contraseña o el código, se mira la página sin escribir nada más: `ok` con `recovered: true` si ya no pide nada, o `ERROR` `browser_unavailable` con `asks: "password" | "code"` si aún lo pide (desde el 17-10-2026) |
| `browser_save_password` | `account` opcional (usa el dominio actual) | Lo mismo que `browser_login` y, con `ok`, `saved`. La contraseña nueva la escribe Pablo en el chat (evento `secret` con `kind: "password"`), se prueba una sola vez y solo se guarda en la entrada existente del baúl si entra. No crea entradas |
| `browser_attach` | `file` (nombre en la carpeta de adjuntos), `field` (texto del campo) | `{status}` |
| `browser_screenshot` | — | `{sent: true}`; la imagen va al chat, no al modelo |
| `browser_sessions` | — | `[{domain, since}]` |
| `browser_logout` | `domain` o `all` | `{closed: [...]}` |
| `browser_log` | `since` | `[{task, sites, effects, status, steps, seconds, cost_usd}]` |

`status` de `browser_task`: `SUBTASK_COMPLETE`, `NEEDS_INPUT`, `NEEDS_AGENT`, `BLOCKED`, `DENIED` (Pablo no permitió), `LIMIT` (pasos o tiempo), `QUOTA` (tope de Command Code), `CANCELLED`.

**Ahorro de tokens y de turnos (cambio del 10-10-2026, pedido por Pablo).** Medido en una página real, cada resultado de `browser_task` llevaba a Claude la tabla de elementos que JEV ya había leído (unos 14 KB) y una lectura devolvía siempre hasta 8.000 caracteres. Desde este cambio:

- `page` dice solo dónde ha quedado la página: dirección, título, número de elementos y si hay más abajo. La tabla de elementos se queda en el ejecutor, para JEV.
- `find` (términos separados por comas, sin distinguir mayúsculas ni tildes) devuelve solo las líneas que los contienen, con una de contexto. Vale en `browser_open`, `browser_read` y `browser_task`.
- `browser_open` devuelve ya el principio del texto, para no gastar un turno en leer.
- El puente entrega cada resultado como un único texto JSON compacto, sin campos vacíos, sin métricas (siguen en `browser_log`) y sin copia estructurada.

Las descargas completadas durante una operación se envían solas al chat y aparecen en la salida como `downloads: [{name, bytes, sent}]`.

### 4.2 API del ejecutor

Todas con `Authorization: Bearer <token>`.

| Método y ruta | Uso |
|---|---|
| `POST /v1/operations` | Cuerpo `{conversation, op, args}`. Responde en flujo, una línea JSON por evento |
| `POST /v1/operations/{id}/answers` | `{event, value}`: respuesta a un evento que espera |
| `POST /v1/operations/{id}/cancel` | Cancela en el siguiente límite de paso |
| `PUT /v1/uploads/{name}` | El puente deja un archivo para adjuntar |
| `GET /v1/downloads/{id}` | El puente recoge una descarga |
| `GET /v1/journal?since=` | Registro |

Eventos del flujo:

| `type` | Campos | Espera respuesta |
|---|---|---|
| `queued` | `position` | no |
| `step` | `n`, `action`, `target`, `ms` | no |
| `approval` | `category` (`enviar`, `borrar`, `dinero`), `domain`, `action`, `fields[]`, `png` | sí: `deny` o `once` |
| `secret` | `kind: "code"`, `domain` | sí: el código, o caduca a los 5 minutos |
| `screenshot` | `png` | no |
| `download` | `id`, `name`, `bytes` | no |
| `done` | resultado de la operación y métricas | no |

«Permitir siempre» lo resuelve el puente: si existe la regla `navegador:enviar:<dominio>`, responde `once` sin preguntar. El ejecutor no guarda permisos.

### 4.3 Configuración

Ejecutor (`/etc/lisa-browser.toml`): `listen`, `cdp_endpoint`, `jev.base_url`, `jev.model`, `jev.price_per_mtok`, `vault.server`, `limits.max_actions`, `limits.operation_seconds`, `limits.approval_seconds` (600), `limits.code_seconds` (300), `money_domains[]`, `paths.downloads`, `paths.uploads`, `paths.journal`.

Puente (`[browser]`): `api_url` (`http://10.0.2.2:8787`), `token_file`, `mcp_port`.

### 4.4 Registro

```sql
CREATE TABLE operations (
  id TEXT PRIMARY KEY, conversation TEXT, op TEXT, goal TEXT,
  started_at TEXT, seconds REAL, status TEXT, steps INTEGER,
  input_tokens INTEGER, cost_usd REAL, sites TEXT
);
CREATE TABLE steps (
  operation_id TEXT, n INTEGER, action TEXT, role TEXT, name TEXT,
  ms INTEGER, decided_by TEXT            -- 'jev' | 'regla'
);
CREATE TABLE effects (
  operation_id TEXT, at TEXT, category TEXT, domain TEXT, action TEXT,
  decision TEXT                          -- 'once' | 'always' | 'deny' | 'expired'
);
```

Nunca se escriben valores de `inputs`, contraseñas, códigos ni imágenes. Los nombres de los controles pasan por la misma redacción de secretos que usa arc-cua.

## 5. Barrera de aprobaciones

Se implementa como un envoltorio del backend: **toda** acción pasa por `execute()`, incluido escribir y pulsar Intro. A arc-cua se le pasan todos sus `allowed_risks` para que su barrera en inglés no interfiera.

Clasificación de cada acción antes de ejecutarla:

| Señal | Categoría |
|---|---|
| El dominio está en `money_domains` y la acción activa un botón, un envío o Intro en un formulario, salvo los reconocidos como inofensivos (buscar, filtrar, siguiente, ver, cerrar aviso) | `dinero` |
| La etiqueta contiene palabras de pago en español, gallego o inglés (pagar, comprar, transferir, confirmar pago, pay, buy…) | `dinero` |
| La etiqueta contiene palabras de borrado (eliminar, borrar, suprimir, dar de baja, delete, remove…) | `borrar` |
| La etiqueta contiene palabras de envío (enviar, publicar, compartir, solicitar, reservar, confirmar, send, post, submit…) | `enviar` |
| Envío de un formulario que no es de búsqueda (`GET` o `role=search`) ni de inicio de sesión | `enviar` |
| Lo demás | ninguna |

Al clasificar con efecto:

1. El ejecutor toma una captura y los campos rellenos del formulario que contiene el control. Para `dinero`, si no encuentra campos con valor, no pide aprobación: termina con `NEEDS_AGENT` y lo explica (R8).
2. Calcula una huella: dirección, control y valores de esos campos.
3. Emite `approval` y espera hasta 10 minutos.
4. Con `once`, vuelve a observar. Si la huella cambió, pide aprobación de nuevo. Si no, ejecuta.
5. Con `deny` o al caducar, no ejecuta y la subtarea termina en `DENIED`.

Tras ejecutar una acción `dinero`, el ejecutor no reintenta nada: observa, toma captura y devuelve el control a Claude con el aviso de no repetir (R8).

Límite conocido y aceptado por Pablo: un control sin texto reconocible fuera de un formulario puede no detectarse.

## 6. Inicio de sesión y segundo factor

1. `browser_login` busca en el baúl las entradas cuya dirección coincide con el dominio registrable de la página (`tldextract`). Sin coincidencia: `no_credentials`. Si la página pide contraseña en un dominio que no coincide con ninguna entrada: `wrong_domain`, sin escribir nada.
2. Lanza una subtarea con `inputs = {usuario, contraseña}` y ambos en `secret_inputs`. Un solo intento: si tras enviar sigue habiendo campo de contraseña o aparece un error, `rejected`.
3. Si la subtarea devuelve `NEEDS_INPUT` sobre un campo de código (por `autocomplete=one-time-code` o por su etiqueta), el ejecutor usa el generador de la entrada si existe; si no, emite `secret` y espera 5 minutos. Dos intentos como máximo.
4. Con éxito, anota el dominio como sesión abierta.

El inicio de sesión no pasa por la barrera de aprobaciones. La contraseña maestra del baúl se lee de un archivo de secretos al arrancar; la clave de sesión de `bw` queda solo en la memoria del ejecutor.

## 7. Errores y límites

| Situación | Resultado |
|---|---|
| JEV no responde o devuelve error | `NEEDS_AGENT` con el motivo; no se ejecuta nada |
| 403 o 429 persistente de Command Code | `QUOTA`; el puente avisa a Pablo y no ofrece alternativa de pago |
| Se alcanzan `max_actions` o `operation_seconds` | `LIMIT` |
| El contenedor no responde | La operación falla con `browser_unavailable`; no se usa otro navegador |
| El objetivo cambió entre observar y actuar | arc-cua no actúa (`stale`); se vuelve a decidir |
| La página abre un diálogo nativo | Lo gestiona arc-cua como un elemento más |
| Marco de otro origen, lienzo sin elementos | `NEEDS_AGENT`; Claude puede pedir captura al chat, no al modelo, y preguntar a Pablo |

Las esperas a Pablo (aprobación, código) no cuentan para `operation_seconds`.

## 8. Restricciones

- Nada de este trabajo toca la memoria ni le da al contenedor acceso a ella.
- El ejecutor no acepta peticiones sin token y solo escucha en `127.0.0.1`.
- Las capturas viven en memoria; el puente borra el archivo temporal tras enviarlo.
- No se añade ningún servicio de terceros aparte de Command Code.
- arc-cua se usa sin modificar. Lo que le falta se añade alrededor (transporte, barrera, subidas, descargas).
- Código en inglés, documentación y mensajes al usuario en español, pruebas con `unittest`, como en `lisa-bridge`.

## 9. Fuera de alcance

Lo que excluye la SPEC, y además: marcos de otro origen, arrastrar y soltar, varias pestañas a la vez, y el caché de acciones.

## 10. Orden de integración

1. Transporte de JEV y sitio de pruebas con Chromium real: una subtarea de punta a punta sin puente.
2. Barrera, inicio de sesión con baúl ficticio, subidas, descargas y registro.
3. API del ejecutor.
4. Puente: canal, respuesta secreta y servidor MCP, probado contra un ejecutor ficticio.
5. Servidor: imagen, contenedor, servicio y cortafuegos por usuario.
6. lisa-av: configuración e instrucciones. Primera tarea real desde el chat.
7. Baúl real y pruebas reales con Pablo.

## 11. Traducción de las aceptaciones

Las pruebas automáticas usan un sitio local servido por las propias pruebas, Chromium sin interfaz, `ScriptedPolicy` en lugar de JEV y un baúl ficticio. Se ejecutan desde `packages/lisa-browser` con `PYTHONPATH=src python -m unittest <módulo> -v`, o desde `packages/lisa-bridge` las marcadas (puente).

Las «pruebas reales» se hacen desde el chat de Lisa con JEV, el contenedor y el baúl de verdad; su evidencia es el registro del ejecutor y lo que Pablo ve en el chat.

| Aceptación | Prueba |
|---|---|
| R1.1 dato correcto de web pública | `tests.test_tasks.PublicLookup`; prueba real con una pregunta que Pablo verifica |
| R1.2 página inexistente | `tests.test_tasks.MissingPage` |
| R1.3 límite de pasos | `tests.test_tasks.StepLimit` |
| R2.1 decide JEV, sin capturas a modelos | `tests.test_jev.RequestHasNoImages`; prueba real: `steps.decided_by = 'jev'` en todas las filas |
| R2.2 pasos, tiempo y coste registrados | `tests.test_journal.Metrics`; prueba real con `browser_log` |
| R2.3 tope alcanzado | `tests.test_jev.QuotaStopsTask` |
| R3.1 entra en la USC | prueba real |
| R3.2 la contraseña no aparece en ningún sitio | `tests.test_login.SecretNeverLeaves` (busca la contraseña en registro, eventos, peticiones a JEV y captura); prueba real: búsqueda en el registro y en el historial del puente |
| R3.3 otro dominio | `tests.test_login.WrongDomain` |
| R3.4 un solo intento | `tests.test_login.SingleAttempt` |
| R4.1 código por el chat y mensaje borrado | (puente) `tests.test_browser.CodeReplyIsConsumed`; prueba real |
| R4.2 el código no se guarda | (puente) `tests.test_browser.CodeNotStored`; `tests.test_login.CodeNotInJournal` |
| R4.3 caduca a los 5 minutos | `tests.test_login.CodeTimeout`, con reloj simulado |
| R5.1 sesión al día siguiente | prueba real |
| R5.2 sesión tras reiniciar | prueba real: `sudo systemctl restart docker-lisa-navegador` y nueva tarea |
| R5.3 cerrar sesión | `tests.test_session.LogoutClearsCookies`; prueba real |
| R6.1 factura del operador | prueba real (aplazada: falta el operador) |
| R6.2 subir una foto | `tests.test_files.Upload`; `tests.test_files.Download` cubre la descarga |
| R7.1 publicar con aprobación | `tests.test_gate.SendAsksAndRuns`; prueba real (aplazada: falta el sitio) |
| R7.2 denegar | `tests.test_gate.DenyStops` |
| R7.3 siempre para enviar, nunca para borrar | (puente) `tests.test_browser.AlwaysRuleForSend`, `tests.test_browser.DeleteNeverOffersAlways` |
| R7.4 texto de la página que ordena enviar | `tests.test_gate.InjectedTextNeedsApproval` |
| R8.1 operación real | prueba real (aplazada por Pablo: sin bancos durante el desarrollo) |
| R8.2 fallo tras confirmar | `tests.test_gate.MoneyNeverRetried`; `tests.test_gate.MoneyWithoutFieldsStops` |
| R9.1 capturas en las tareas | (puente) `tests.test_browser.ApprovalCarriesPhoto`; prueba real |
| R9.2 contraseña no visible | `tests.test_session.ScreenshotHidesPassword` (compara la captura con y sin valor en el campo) |
| R10.1 falta un dato | `tests.test_tasks.NeedsInputThenContinues` |
| R10.2 captcha de imágenes | `tests.test_tasks.ImageCaptchaBlocks` |
| R11.1 aislamiento del contenedor | En el servidor: `docker exec lisa-navegador ls /srv/lisa /var/lib/lisa-secretos` debe fallar; `docker inspect` no muestra el socket de Docker ni puertos publicados |
| R11.2 puerto de control inalcanzable | Desde otro equipo de casa, desde otro contenedor y desde lisa-av como `lisa-agente`: conexión al puerto de control rechazada |
| R12.1 lista lo hecho | `tests.test_journal.ListsEffects`; prueba real con «¿qué hiciste hoy en el navegador?» |
| R12.2 sin secretos en el registro | `tests.test_journal.NoSecrets` |

## 12. Biblioteca: sitios y rutinas (R13, 12-10-2026)

**Dónde vive.** En el ejecutor (`library.py`), en un SQLite propio junto al registro (`biblioteca.sqlite`; `paths.library` lo cambia). Está aquí y no en el agente porque es el ejecutor quien puede hacer una rutina entera sin el planificador, y porque así la comparten todos los canales (el chat web y la orden `lisa` del ordenador de Pablo).

**Qué guarda.**

| Tabla | Campos |
|---|---|
| `sites` | nombre, dirección, dominio, notas (hasta 600 caracteres), alias, veces usado |
| `routines` | nombre, descripción, datos que necesita, pasos (JSON), veces ejecutada, veces que salió bien, último estado |

Los nombres se comparan sin mayúsculas, tildes ni espacios de más. Un paso de una rutina es una de las operaciones `open`, `task`, `login` o `read` con sus argumentos; los `{dato}` se sustituyen al ejecutar. Solo se guardan los campos conocidos de cada operación.

**API.** No pasa por la cola de operaciones, porque no toca el navegador:

| Petición | Qué hace |
|---|---|
| `GET /v1/library` | Sitios y rutinas, sin los pasos: es lo que el planificador lleva en cada mensaje |
| `PUT /v1/library/sites/{nombre}` · `DELETE` | Guarda (`url`, `notes`, `aliases`) o borra un sitio |
| `GET /v1/library/routines/{nombre}` | Una rutina con sus pasos |
| `PUT /v1/library/routines/{nombre}` · `DELETE` | Guarda (`description`, `steps`) o borra una rutina |

Los datos inválidos responden 400 con `reason`, que va al planificador.

**Ejecución.** Operación nueva `routine` con `{name, params}`. Hace los pasos en orden dentro de una sola operación del registro:

- Emite un evento `step` por paso (`rutina 2/3: task`) además de los de JEV.
- `login` se salta si la página no muestra un campo de contraseña: la sesión del sitio se conserva (R5).
- El texto de un paso intermedio solo vuelve si el paso lo pide con `find`; el del último paso vuelve siempre.
- Un paso vale para seguir si `open` cargó, `task` terminó en `SUBTASK_COMPLETE` o `login` en `ok`.
- Resultado: `ok` con la dirección y el título finales y el texto pedido; `ROUTINE_FAILED` con `failed_step`, `of`, el paso y su resultado; `NEEDS_INPUT` con `missing`; `no_routine` con las que hay; `CANCELLED`.

La barrera de aprobaciones y la redacción de secretos son las de cada operación: una rutina no las rodea.

**Qué no hace.** No graba clics para repetirlos sin JEV, no aprende rutinas sola (las guarda el planificador cuando una gestión sale bien) y no adjunta archivos ni cierra sesiones.

## 13. Enlaces con dirección y `follow` (12-10-2026)

En la primera gestión real, casi todo lo que el planificador encargó a JEV fue «pulsa el enlace que se llama X», y ahí estuvieron los fallos: una tarjeta de curso que no respondía al clic (`BLOCKED`) y un enlace equivocado (la práctica 1 en vez de la 2). Para seguir un enlace no hace falta un modelo.

- `open` y `read` aceptan `links: true` y devuelven, además del texto, los enlaces visibles con su dirección (los que casan con `find`, hasta 15). El planificador puede abrir uno él mismo.
- Operación `follow` con `{text}`: abre el enlace visible que se llama así. Gana el que coincide exactamente; si no hay, el que empieza así; si no, el que lo contiene, siempre sin tildes ni mayúsculas. Si en el mejor grupo hay enlaces a sitios distintos no elige: devuelve `ambiguous_link` con los candidatos. Admite `find` y `read_chars` como `open`.
- `follow` es también un paso de rutina. Una rutina hecha de `open` y `follow` no llama a JEV.

Los enlaces sin dirección web (`javascript:`, menús que despliegan) no entran: para esos sigue estando `task`.

## 14. Una biblioteca que puede crecer (12-10-2026)

Guardar una rutina no cuesta disco (unos 500 bytes), pero cada una que el planificador lleva en sus instrucciones se paga en todas sus llamadas (unos 50 tokens), y con muchas parecidas elige peor. Es el mismo problema que tener demasiadas herramientas.

`GET /v1/library` acepta dos parámetros:

- `limit`: devuelve solo lo más usado, hasta ese número de sitios y de rutinas, y en `more` cuánto queda fuera. Es una lista **estable**: no depende de lo que se pregunte, así que lo que el planificador lleva en cada mensaje sigue valiendo en la caché de su proveedor. Reordenarla por relevancia en cada mensaje invalidaría esa caché y saldría más caro que llevar la lista entera.
- `q`: la petición del usuario. Añade en `matches` lo guardado que encaja con ella y no estaba ya en la lista (hasta 5 sitios y 5 rutinas). El planificador lo recibe junto al mensaje, no en sus instrucciones. Se puntúa por palabras, sin tildes: pesa más el nombre y los alias que la descripción. Con unos cientos de entradas no hace falta nada más; si algún día no basta, aquí iría una búsqueda semántica.

Una rutina que falla tres veces seguidas queda **dormida**: deja de salir en la lista, pero sigue guardada, se encuentra si encaja y se despierta en cuanto vuelve a salir bien.

