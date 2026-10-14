# Tareas — Browser Use (CU02)

> Este documento se escribió en `lisa-project` (`specs/browser-use/`), cuando el ejecutor era el paquete
> `packages/lisa-browser` de aquel repositorio. Donde dice `packages/lisa-browser/…`, léase la raíz de este;
> los enlaces a `docs/NN` apuntan allí.

De [SPEC.md](SPEC.md) y [PLAN.md](PLAN.md). Los estados y la evidencia los escribe quien construye, tras ejecutar la aceptación.

Entorno de las aceptaciones de `packages/lisa-browser`: se ejecutan en el servidor Lisa, dentro de `nix-shell` (el `shell.nix` del paquete trae Python con arc-cua y Chromium). El código se copia al servidor a una carpeta temporal; no se despliega nada.

Durante el desarrollo no se entra en bancos ni en sitios reales con efecto (decisión de Pablo del 10-10-2026).

## Tanda 1

### T1 — Transporte de JEV y ejecución de subtareas con límites
- Estado: verificada
- Requisitos: R1 (límites), R2
- Depende de: — (usa el entorno de T3)
- Archivos propios: `packages/lisa-browser/src/lisa_browser/jev.py`, `packages/lisa-browser/src/lisa_browser/runner.py`, `packages/lisa-browser/tests/test_jev.py`, `packages/lisa-browser/tests/test_runner.py`
- Punto de revisión: no
- Briefing: `CommandCodeJevTransport` cumple el transporte de `arc_cua.policies.choice.ChoicePolicy`: `ask(state, questions, *, images=())` hace `POST {base_url}` con `{"model", "state", "questions"}` y `Authorization: Bearer`; rechaza `images`; acumula `usage.input_tokens`, número de llamadas y milisegundos; reintenta 429/503/529 hasta 3 veces; un 403 o un 429 persistente lanza `QuotaExceeded`; cualquier otro error lanza `RuntimeError` sin ejecutar nada. `run_subtask(backend, policy, payload, *, limits, on_step, cancel)` ejecuta `DesktopExecutor.run_iter` y devuelve `{status, reason, needs_input, actions_taken, page, metrics}` con `status` ∈ `SUBTASK_COMPLETE, NEEDS_INPUT, NEEDS_AGENT, BLOCKED, LIMIT, QUOTA, CANCELLED`. `max_actions` se limita a 40. A arc-cua se le pasan todos sus `allowed_risks`. Los secretos de `secret_inputs` no aparecen en la salida.
- Aceptación:
  1. `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_jev tests.test_runner -v"`
- Evidencia: `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_jev tests.test_runner -v"` → código 0, `Ran 16 tests in 14.285s — OK` (servidor Lisa, 10-10-2026). Comprobación adicional fuera de la aceptación: una subtarea real con JEV por Command Code sobre el sitio de pruebas en Chromium («ir a la página de horarios») terminó en `SUBTASK_COMPLETE` con 1 acción, 2 llamadas a JEV (2,3 s en total), 4.892 tokens de entrada y 0,0002 $.

### T2 — Registro de operaciones
- Estado: verificada
- Requisitos: R2 (métricas), R12
- Depende de: — (usa el entorno de T3)
- Archivos propios: `packages/lisa-browser/src/lisa_browser/journal.py`, `packages/lisa-browser/tests/test_journal.py`
- Punto de revisión: no
- Briefing: `Journal(path)` con el esquema de PLAN §4.4 (`operations`, `steps`, `effects`). `start(conversation, op, goal) -> id`, `step(id, n, action, role, name, ms, decided_by)`, `effect(id, category, domain, action, decision)`, `finish(id, status, seconds, input_tokens, cost_usd, sites)`, `summary(since) -> list`. Todo texto pasa por una redacción con la lista de secretos de la operación. No guarda valores de `inputs`, códigos ni imágenes.
- Aceptación:
  1. `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_journal -v"`
- Evidencia: `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_journal -v"` → código 0, `Ran 4 tests in 0.055s — OK` (servidor Lisa, 10-10-2026).

### T3 — Entorno, sitio de pruebas y sesión de Chromium
- Estado: verificada
- Requisitos: R1 (abrir y leer), R9
- Depende de: —
- Archivos propios: `packages/lisa-browser/shell.nix`, `packages/lisa-browser/nix/arc-cua.nix`, `packages/lisa-browser/pyproject.toml`, `packages/lisa-browser/README.md`, `packages/lisa-browser/src/lisa_browser/__init__.py`, `packages/lisa-browser/src/lisa_browser/session.py`, `packages/lisa-browser/tests/__init__.py`, `packages/lisa-browser/tests/site.py`, `packages/lisa-browser/tests/support.py`, `packages/lisa-browser/tests/test_session.py`
- Punto de revisión: no
- Briefing: `shell.nix` da Python con `arc-cua==0.1.1` (rueda de PyPI fijada por hash), `aiohttp`, `httpx[http2]`, `websockets`, `tldextract`, y Chromium. `Browser` envuelve `ChromeBackend`: `Browser.launch(...)` para pruebas y `Browser.connect(endpoint)` para el contenedor; `open(url) -> {url, title, status, ok}` sin lanzar excepción ante 404 o error de red; `read(max_chars) -> {url, title, text, truncated}`; `screenshot() -> bytes` PNG. `tests/site.py` sirve páginas de prueba desde las propias pruebas. `tests/support.py` arranca Chromium sin interfaz y trae una política guionizada que elige elementos por nombre.
- Aceptación:
  1. `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_session -v"`
- Evidencia: `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_session -v"` → código 0, `Ran 5 tests in 4.045s — OK`, con Chromium 154 sin interfaz (servidor Lisa, 10-10-2026).

## Tanda 2

### T4 — Tareas de punta a punta en el sitio de pruebas
- Estado: verificada
- Requisitos: R1, R10
- Depende de: T1, T3
- Archivos propios: `packages/lisa-browser/tests/test_tasks.py`
- Punto de revisión: no
- Briefing: Con `Browser` real y política guionizada sobre el sitio de pruebas: `PublicLookup` (navega y lee un dato), `MissingPage`, `StepLimit`, `NeedsInputThenContinues`, `ImageCaptchaBlocks`. No añade código de producción salvo correcciones en archivos de T1/T3 que las pruebas destapen, que se anotan en la evidencia.
- Aceptación:
  1. `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_tasks -v"`
- Evidencia: `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_tasks -v"` → código 0, `Ran 5 tests — OK` (servidor Lisa, 10-10-2026). Cambios en archivos de T3 que las pruebas necesitaron: `tests/support.py` arranca Chromium con los nombres `*.test` apuntando al sitio de pruebas; `tests/site.py` gana `url(path, host)` y `posts()`.

### T5 — Barrera de aprobaciones
- Estado: verificada
- Requisitos: R7, R8
- Depende de: T1, T3
- Archivos propios: `packages/lisa-browser/src/lisa_browser/gate.py`, `packages/lisa-browser/tests/test_gate.py`
- Punto de revisión: no
- Briefing: PLAN §5 completo. `classify(action, element, form_context, domain) -> None | "enviar" | "borrar" | "dinero"` con las reglas en español, gallego e inglés y los inofensivos en sitios de dinero. `GatedBackend(inner, approve)` envuelve `execute()`: para una acción con efecto reúne captura y campos del formulario, calcula la huella, llama a `approve(request) -> "once" | "deny"` y vuelve a observar antes de ejecutar; si la huella cambió, pide de nuevo. Denegación o caducidad terminan la subtarea con `DENIED`. Tras una acción `dinero` no se reintenta. Sin campos legibles en `dinero`, no se pide aprobación y la subtarea termina en `NEEDS_AGENT`. Pruebas: `SendAsksAndRuns`, `DenyStops`, `InjectedTextNeedsApproval`, `EnterSubmitIsGated`, `SearchAndLoginAreNotGated`, `PageChangeAsksAgain`, `MoneyNeverRetried`, `MoneyWithoutFieldsStops`, `MoneyDomainAsksByDefault`.
- Aceptación:
  1. `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_gate -v"`
- Evidencia: `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_gate -v"` → código 0, `Ran 17 tests — OK` (servidor Lisa, 10-10-2026). Comprobado además que las pruebas fallan al quitar la clasificación (10 de 17), la revisión tras aprobar (1) y las dos paradas tras una operación de dinero (1). Desviación del plan: `gate.py` recibe el dominio por una función inyectable (`domain_of`) en lugar de usar `tldextract` directamente, para no depender de `vault.py`.

### T6 — Baúl, inicio de sesión y segundo factor
- Estado: verificada
- Requisitos: R3, R4
- Depende de: T1, T3
- Archivos propios: `packages/lisa-browser/src/lisa_browser/vault.py`, `packages/lisa-browser/src/lisa_browser/login.py`, `packages/lisa-browser/tests/test_login.py`
- Punto de revisión: no
- Briefing: PLAN §6. Interfaz `Vault` (`find(url) -> list[Entry]`, `Entry{username, password, has_totp, uris}`, `totp(entry) -> str`), `FakeVault` para pruebas y `BitwardenCliVault` (no se prueba aquí contra un servidor real). `login(browser, policy, vault, ask_code) -> status` con `ok`, `no_credentials`, `rejected`, `wrong_domain`, `vault_unavailable`. Coincidencia por dominio registrable con `tldextract`. Un solo intento de contraseña; dos de código; 5 minutos de espera con reloj inyectable. Pruebas: `LogsIn`, `SecretNeverLeaves`, `WrongDomain`, `SingleAttempt`, `CodeFromChat`, `CodeFromVault`, `CodeTimeout`, `CodeNotInJournal`.
- Aceptación:
  1. `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_login -v"`
- Evidencia: `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_login -v"` → código 0, `Ran 16 tests — OK` (servidor Lisa, 10-10-2026). Comprobado que fallan al quitar la escritura única de la contraseña (1), la coincidencia de dominio (4) y la redacción del registro (2). Cambio en un archivo de T2: `journal.py` gana `add_secret()` para ocultar un código que aparece a mitad de la operación. `BitwardenCliVault` solo se ha probado contra un `bw` simulado. Añadido al contrato: `login` devuelve `reason` (`password`, `code`, `code_timeout`) junto a `rejected`, y `expected` junto a `wrong_domain`.

## Tanda 3

### T7 — Archivos y sesiones
- Estado: verificada
- Requisitos: R5, R6, R9
- Depende de: T3
- Archivos propios: `packages/lisa-browser/src/lisa_browser/files.py`, `packages/lisa-browser/tests/test_files.py`, `packages/lisa-browser/tests/test_sessions.py`
- Punto de revisión: no
- Briefing: Subidas con `DOM.setFileInputFiles` sobre el campo cuyo texto se indica; descargas con `Browser.setDownloadBehavior` a una carpeta, devolviendo nombre y tamaño al completarse; lista de dominios con sesión y cierre por dominio borrando cookies y almacenamiento. Pruebas: `Upload`, `Download`, `LogoutClearsCookies`, `ScreenshotHidesPassword`.
- Aceptación:
  1. `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_files tests.test_sessions -v"`
- Evidencia: `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_files tests.test_sessions -v"` → código 0, `Ran 10 tests in 9.642s — OK` (servidor Lisa, 10-10-2026). Comprobado que `ScreenshotHidesPassword` falla al quitar la ocultación. Cambios en archivos de otras tareas: `session.py` (T3) deja en blanco los campos de contraseña y de código durante la captura y espera las promesas de los scripts; `journal.py` (T2) gana la tabla `sessions`; `tests/site.py` (T3) sirve respuestas con cabeceras. Límites: la ocultación no alcanza a los marcos de otro origen (ahí queda la del navegador) ni a un campo de contraseña que la página haya pasado a texto visible; las descargas solo se han probado con archivos pequeños.

### T8 — API del ejecutor
- Estado: verificada
- Requisitos: R1 (cola, cancelar), R2, R12
- Depende de: T1, T2, T5, T6, T7
- Archivos propios: `packages/lisa-browser/src/lisa_browser/server.py`, `packages/lisa-browser/src/lisa_browser/__main__.py`, `packages/lisa-browser/config.example.toml`, `packages/lisa-browser/tests/test_server.py`
- Punto de revisión: no
- Briefing: PLAN §4.2 y §4.3 literales: rutas, eventos y claves de configuración. Token obligatorio; escucha solo en `127.0.0.1`. Una operación a la vez, con evento `queued`. Las esperas a respuestas no cuentan para `operation_seconds`. Cada operación queda en el registro.
- Aceptación:
  1. `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_server -v"`
- Evidencia: `cd packages/lisa-browser && nix-shell --run "python -m unittest tests.test_server -v"` → código 0, `Ran 17 tests in 35.198s — OK`; batería completa del paquete, `discover -s tests -t .` → código 0, `Ran 90 tests in 97.541s — OK` (servidor Lisa, 10-10-2026). En la primera ejecución fallaron 3 de 16: un error real (`Operation.ask` chocaba con el campo `kind` del evento de código) y una prueba con plazos mal elegidos; corregidos. Comprobado que las pruebas fallan al contar la espera como tiempo, al quitar el límite de tiempo, al no exigir el token, al aceptar una subida con ruta de fuera y al tratar una aprobación caducada como aprobada. Cambios en archivos de otras tareas: `runner.py` (T1) aplica el límite de tiempo descontando las esperas; `gate.py` (T5) mide esas esperas. Desviación: en `config.example.toml` los plazos del ejecutor son 630 s y 330 s, algo mayores que los del puente (600 y 300), para que sea el puente quien caduque la pregunta y el ejecutor solo sirva de respaldo. No probado: `__main__.py` (arranque con configuración real, JEV y `bw`), que se ejercita en T10 y T12.

### T9 — Puente: canal, respuesta secreta y herramientas MCP
- Estado: verificada
- Requisitos: R4, R7, R9
- Depende de: T8 (contrato de la API)
- Archivos propios: `packages/lisa-bridge/src/lisa_bridge/browser.py`, `packages/lisa-bridge/tests/test_browser.py`; cambios en `packages/lisa-bridge/src/lisa_bridge/channel.py`, el canal de mensajería, `service.py`, `permissions.py`, `config.py`, `config.example.toml`
- Punto de revisión: no
- Briefing: PLAN §3.2 y §4.1 literales. Pruebas contra un ejecutor ficticio: `CodeReplyIsConsumed`, `CodeNotStored`, `AlwaysRuleForSend`, `DeleteNeverOffersAlways`, `ApprovalCarriesPhoto`, `ToolsAreListed`, `CancelReachesExecutor`. Las 34 pruebas existentes del puente siguen pasando.
- Aceptación:
  1. `cd packages/lisa-bridge && PYTHONPATH=src python -m unittest discover -s tests -v`
- Evidencia: `cd packages/lisa-bridge && PYTHONPATH=src python -m unittest discover -s tests -v` → código 0, `Ran 55 tests in 29.475s — OK` (lisa-av, Python 3.13 con acp 0.10.0, mcp 1.26.0, httpx 0.28.1 y uvicorn 0.40.0; 10-10-2026). Son las 34 anteriores y 21 nuevas. En la primera ejecución fallaron 17 por un error real: FastMCP no podía leer las anotaciones de las herramientas; se resolvió sacando el servidor MCP a un módulo propio. Comprobado con 12 mutaciones que las pruebas fallan al ofrecer «siempre» para borrar, al extender la regla de enviar a borrar o dinero, al no guardar la regla, al aprobar por caducidad, al guardar el código en el historial, al dejarlo llegar al agente, al no borrar el mensaje, al aceptar cualquier token, al no propagar `/cancelar`, al adjuntar rutas de fuera, al no enviar el token al ejecutor y al pedir permiso por las herramientas de navegador. Archivos fuera de la lista: nuevo `browser_mcp.py`; cambios en `agent.py` (servidores MCP por sesión), `__main__.py`, `pyproject.toml`, `tests/fake_agent.py`, `tests/test_units.py` y el flujo de CI. No probado: el canal de mensajería real (fotos con botones, envío de archivos y borrado del mensaje del código solo contra un canal simulado) y Claude real (que conecte el servidor MCP y con qué nombre presenta las herramientas al pedir permiso; se asume `mcp__navegador__*`). Ambas cosas se comprueban en T11.

## Tanda 4

### T10 — Servidor: imagen, contenedor, servicio y cortafuegos
- Estado: en curso
- Requisitos: R11
- Depende de: T8
- Archivos propios: en el repositorio `lisa`: `hosts/lisa/navegador.nix`, `flake.nix`, `README.md`
- Punto de revisión: sí — cambia el cortafuegos del servidor y lo que lisa-av puede alcanzar. Antes de hacerlo permanente se aplica con `nixos-rebuild test` y Pablo confirma que sigue entrando por SSH al servidor y a lisa-av.
- Briefing: PLAN §3.3. Necesita orden de Pablo para subir `lisa-project` a GitHub (el servidor ejecuta el código desde un clon) y para guardar los secretos del servidor.
- Aceptación:
  1. En el servidor: `docker exec lisa-navegador ls /srv/lisa /var/lib/lisa-secretos` falla; `docker inspect lisa-navegador` no muestra puertos publicados ni el socket de Docker.
  2. El puerto de control no responde desde otro equipo de casa, desde otro contenedor ni desde lisa-av como `lisa-agente`.
  3. Desde lisa-av, el puerto 22 y el 8787 del servidor responden y Vaultwarden (8222) no.
  4. `ssh -J pablo@lisa -p 2222 pablo@127.0.0.1` sigue entrando en lisa-av.
- Evidencia (parcial, 10-10-2026; repositorio `lisa`, commit `ec43d4b`, sistema en ejecución igual al del repositorio):
  - Aceptación 1: `docker exec lisa-navegador ls /srv/lisa /var/lib/lisa-secretos` → código 127 (la imagen ni siquiera trae `ls`); abrir esas rutas y el socket de Docker desde dentro da «No such file or directory». `docker inspect`: sin puertos, sin privilegios, raíz de solo lectura, todas las capacidades quitadas, usuario 961 y solo tres montajes (perfil, descargas y subidas, esta de solo lectura).
  - Aceptación 2: el puerto de control (172.30.0.2:9223) no responde desde otro contenedor de la red por defecto, desde un equipo externo simulado (espacio de red aparte, tráfico reenviado), desde el usuario `pablo` del servidor ni desde lisa-av como `lisa-agente` o `lisa-puente`; responde al ejecutor (200). No se ha probado desde un equipo físico de la red de casa.
  - Aceptación 3: **pendiente** de la revisión de Pablo. Hoy, desde lisa-av responden el 22, el 8787 (401 sin token) y también Vaultwarden en el 8222 (200), que es lo que la regla debe cerrar.
  - Aceptación 4: `ssh -J pablo@lisa -p 2222 pablo@127.0.0.1 hostname` → `lisa-asistente-personal`.
  - Además: el aislamiento de procesos de Chromium está activo (el proceso de página corre en espacios de usuario y de procesos propios, con dos filtros de llamadas); desde el contenedor solo se alcanza internet (cerrados el servidor, la red de casa, Tailscale y otras redes de Docker); descarga, subida y captura reales correctas con usuarios distintos para navegador y ejecutor; con el contenedor parado el ejecutor responde `browser_unavailable` y se recupera al arrancarlo. Una tarea real con JEV en Wikipedia ejecutó 3 pasos (0,4–1,8 s cada uno) pero no llegó al objetivo y terminó en `BLOCKED`: con un encargo amplio en una página con cientos de enlaces, JEV se pierde.
  - Desviaciones del plan: usuarios distintos para el proceso del navegador (`lisa-chromium`) y el ejecutor (`lisa-navegador`); `socat` dentro de la imagen porque Chromium solo abre su puerto de control en 127.0.0.1; perfil de llamadas al sistema propio (el de Docker más `clone`, `unshare`, `setns` y `chroot`) para no desactivar el aislamiento de Chromium; resolución de nombres pública (9.9.9.9 y 1.1.1.1) en el contenedor; `pkgs/arc-cua.nix` copiado al repositorio `lisa`; el contenedor tampoco alcanza la red de casa ni Tailscale, y el puerto de control se limita también a los procesos del propio servidor. La regla que limita a la máquina virtual está escrita y desactivada (`limitarMaquinaVirtual`).

### T11 — lisa-av: configuración e instrucciones
- Estado: en curso
- Requisitos: R1, R2
- Depende de: T9, T10
- Archivos propios: en el repositorio `lisa-asistente-personal`: `hosts/lisa-asistente-personal/asistente.nix`, `hosts/lisa-asistente-personal/instrucciones.md`
- Punto de revisión: sí — primera tarea real desde el chat en una web pública, hecha por Pablo.
- Briefing: PLAN §3.4.
- Aceptación:
  1. Pablo pide en el chat un dato de una web pública que puede comprobar a mano y recibe el dato, la dirección y una captura.
  2. `browser_log` muestra la tarea con `decided_by = 'jev'` en todos sus pasos, tiempo por paso y coste.
- Evidencia (parcial, 10-10-2026; repositorio `lisa-asistente-personal`, commit `5d99ea3`): puente activo con el servidor de herramientas en 127.0.0.1:8790 y el código de la rama `browser-use`. Comprobación real sin el canal de mensajería (segundo puente con canal en memoria, Claude real por ACP y el ejecutor del servidor): «Abre https://example.com con el navegador y dime el título…» → respuesta correcta («Example Domain» y la primera frase) y una captura de 57.659 bytes en 13,5 s, sin ninguna petición de permiso: Claude conecta el servidor MCP y sus herramientas no preguntan. Se desplegó antes de cerrar T10 porque lo único pendiente allí es la regla de la máquina virtual, de la que T11 no depende. **Pendiente:** las dos aceptaciones, que son la prueba de Pablo desde el chat; `decided_by = 'jev'` solo aparecerá si la tarea necesita `browser_task`.

### T13 — Ahorro de tokens y de turnos de Claude
- Estado: verificada
- Requisitos: R1, R2 (sin cambio de requisitos; cambia el contrato de PLAN §4.1)
- Depende de: T8, T9
- Archivos propios: cambios en `packages/lisa-browser/src/lisa_browser/session.py` y `server.py`, `packages/lisa-bridge/src/lisa_bridge/browser_mcp.py` y `browser.py`, sus pruebas, y el apartado «Navegador» de `instrucciones.md` en `lisa-asistente-personal`
- Punto de revisión: no
- Briefing: pedido por Pablo el 10-10-2026 tras medir el consumo real. Resultado de `browser_task` sin la tabla de elementos; lectura con `find` y `offset`; `browser_open` devuelve el principio del texto; resultados compactos hacia el agente. JEV sigue decidiendo cada acción sobre los elementos de la pantalla: no cambia.
- Aceptación:
  1. `cd packages/lisa-browser && nix-shell --run "python -m unittest discover -s tests -t . -v"`
  2. `cd packages/lisa-bridge && PYTHONPATH=src python -m unittest discover -s tests -v`
  3. En la misma página real de la medida inicial, el resultado de un paso de `browser_task` ocupa menos de 1 KB.
- Evidencia (10-10-2026): aceptación 1 → `Ran 101 tests — OK` (servidor Lisa); aceptación 2 → `Ran 57 tests — OK` (lisa-av). Comprobado con 7 mutaciones que las pruebas nuevas fallan al quitar cada ahorro (lista entera, abrir sin texto, búsqueda que distingue tildes, búsqueda sin límite, métricas, copia estructurada, campos vacíos). Aceptación 3, en el artículo «Santiago de Compostela» de Wikipedia: el resultado de un paso pasa de 13.850 bytes a 227 hacia Claude; leer sin filtro, de 8.599 a 4.461; leer con `find` devuelve 1.149 bytes de una página de 76.873 caracteres; abrir pasa de 236 a 1.917 bytes porque ya incluye el principio del texto. Con Claude real (puente de prueba con canal en memoria): «¿Cuántos habitantes tiene Santiago de Compostela…?» se resolvió con una sola llamada al navegador en 4,5 s; la consulta de example.com, con 2 llamadas en vez de 3 (12,8 s frente a 13,5 s). Desplegado en el servidor y en lisa-av (rama `browser-use`, commit `1714cd7`; instrucciones en `lisa-asistente-personal` `0d6ec1a`). No se hizo el cambio previsto de «enseñar a JEV solo la pantalla»: arc-cua ya lo hace así.

## Tanda 5

### T12 — Baúl real y pruebas reales
- Estado: en curso
- Requisitos: R3, R4, R5
- Depende de: T11
- Archivos propios: ninguno nuevo; correcciones en `vault.py` si `bw` no funciona contra Vaultwarden
- Punto de revisión: sí — necesita que Pablo guarde por terminal la contraseña maestra y la clave de API del baúl, y que esté presente para el segundo factor.
- Briefing: PLAN §10 paso 7. Sin bancos ni sitios de dinero.
- Aceptación:
  1. Lisa entra en el campus virtual de la USC y devuelve un dato que solo se ve con sesión iniciada.
  2. La contraseña usada no aparece en el registro del ejecutor ni en el historial del puente.
  3. Al día siguiente, una segunda tarea no vuelve a pedir contraseña; tras `sudo systemctl restart docker-lisa-navegador`, la sesión sigue abierta.
- Evidencia (parcial, 10-10-2026): Pablo guardó las credenciales del baúl en el servidor (la contraseña maestra, por un formulario privado de un solo uso en la red de Tailscale; la clave de API la pegó en el chat y debe rotarla). `bw` 2026.4.2 funciona contra Vaultwarden 1.37 con el usuario del ejecutor: inicio de sesión con clave de API, desbloqueo y lectura (369 entradas). Baúl activado en el ejecutor (`conBaul`, repositorio `lisa` `00a09ac`): `login` en example.com → `no_credentials`; en login.microsoftonline.com → `choose_account` con las dos cuentas guardadas, sin escribir nada. Añadido por ese hallazgo: elección de cuenta con `account` (104 pruebas del ejecutor y 57 del puente pasan). **Pendiente:** las tres aceptaciones, con Pablo presente para el segundo factor.

## Tanda 6

### T14 — Biblioteca: sitios habituales y rutinas
- Estado: verificada y desplegada (12-10-2026)
- Requisitos: R13
- Depende de: T9
- Archivos propios: `src/lisa_browser/library.py`, cambios en `server.py`, `login.py` y `__main__.py`, `tests/test_library.py`
- Punto de revisión: sí — desplegarlo cambia el servicio del servidor (`codigo` en `hosts/lisa/navegador.nix` del repositorio `lisa`).
- Briefing: PLAN §12.
- Aceptación:
  1. `nix-shell --run "python -m unittest discover -s tests -t ."`
  2. Con el ejecutor real: guardar como rutina una gestión de varios pasos y repetirla con una sola llamada del planificador.
  3. Medir la misma gestión antes y después (turnos del planificador y tokens).
- Evidencia (12-10-2026): aceptación 1 → `Ran 123 tests in 124.401s — OK` (servidor Lisa, en una carpeta de trabajo aparte del servicio), de ellos 16 nuevos: almacén, API, rutina de varios pasos, datos que faltan, inicio de sesión con el baúl y salto cuando ya hay sesión, paso que falla, aprobación dentro de una rutina. Desplegada el 12-10-2026 (repositorio `lisa`, `c02ba63`). Aceptaciones 2 y 3, con Claude Sonnet 5.5 de planificador y la gestión «descargar el enunciado de la práctica 2 de Sistemas Operativos I del campus virtual»: sin nada guardado, 20 llamadas del planificador, 242 s y 341.000 tokens sin llegar al campus; con el sitio guardado, 15 llamadas, 73 s y 260.000 tokens; con la rutina, 1 llamada, 14 s y 26.000 tokens (dos pasadas). La rutina la guardó el propio planificador cuando se le pidió. Esa gestión destapó dos fallos, ya corregidos: un PDF abierto en el visor no llegaba como descarga, y pedirlo por segunda vez tampoco.

## Aplazado por decisión de Pablo

Pruebas reales de R6.1 (factura del operador), R7.1–3 (Vinted u otro sitio) y R8.1 (operación de dinero).
