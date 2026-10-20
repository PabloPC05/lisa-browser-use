# MCP del navegador

Desde el 17-10-2026 el navegador se usa como un MCP: el destino `browser` del MCP único de Pablo (`https://mcp.example.com/todo/mcp`). Así lo maneja cualquier agente que Pablo conecte (Claude Code, claude.ai, Codex…), y no solo el chat de Lisa, que se retiró ese día junto con lisa-agent.

## Decisiones de Pablo (17-10-2026)

- **Lo que hay que preguntarle se pregunta en la ventana de su agente**, con un formulario MCP (*elicitation*): el permiso para enviar, borrar o pagar, el código de un segundo factor y una contraseña nueva. Lo que escribe en el formulario va del cliente al ejecutor; el modelo no lo ve y no puede contestar por él. Pablo prefirió esto a un aviso en el móvil o a una web con passkey, sabiendo que no todos los clientes admiten formularios y que los códigos pasan por el cliente.
- **El agente ve las capturas**: `screenshot` devuelve la imagen. Antes iba solo al chat, no al modelo.
- **Entra todo lo que ya existía**: navegar, leer, pasos con JEV, inicio de sesión con el baúl, rutinas y archivos. El certificado digital, el pago con tarjeta, el BOE y las sedes oficiales se quitaron el 18-10-2026 (ver el README).
- **Desde clientes sin formularios (claude.ai), los permisos se piden por el chat**, con este alcance: enviar y borrar, y los códigos de verificación, sí; pagar y una contraseña nueva, nunca. Pablo aceptó el riesgo: por el chat, quien pasa su «sí» al navegador es el modelo, y una página con instrucciones escondidas podría engañarlo para darlo por él; y el código lo ve el modelo (es de un solo uso).

## Cómo funciona

```text
agente ──► mcp.example.com (Caddy, puerta con clave) ──► agentgateway (MCP único)
                                                          └─► 127.0.0.1:8789/mcp (este servicio, con su clave)
```

- `mcp_server.py` es un servidor MCP (SDK oficial de Python, Streamable HTTP) y lo único que sirve el servicio. Cada herramienta crea una operación del ejecutor (`executor.py`): una cola (una operación a la vez), la barrera de aprobaciones y el registro.
- Se configura en `[mcp]` (ver `config.example.toml`). Exige `Authorization: Bearer <clave>` (`[mcp].token_file`), que pone el MCP único (su `browser.token`).
- agentgateway antepone `browser_` a cada herramienta: `open` se ve como `browser_open`. Se comprobó el 17-10-2026 que agentgateway 1.6.0 deja pasar los formularios entre el servidor y el cliente, también con varios destinos juntos.

### Eventos del ejecutor

| Evento | Qué hace el MCP |
|---|---|
| `step`, `queued` | Aviso de progreso al cliente, si lo pidió, por la respuesta de la llamada |
| `approval` | Formulario «¿Lo permites?». Con enviar, además «permitir siempre en este sitio» (regla `navegador:enviar:<dominio>` en `reglas.json`; nunca para borrar ni dinero). No contestar, rechazar o dejarlo sin marcar es `deny`. Si el cliente no admite formularios, también |
| `secret` | Formulario con el código o la contraseña nueva. Si no contesta, no se envía nada y el ejecutor lo da por caducado |
| `screenshot` | La imagen va en el resultado |
| `done` | El resultado, en JSON compacto y sin `metrics` (siguen en el registro del servidor). Las descargas de hasta 10 MB van además dentro como recurso. Todas se guardan en el servidor (ver «Descargas») |

Los formularios caducan 15 s antes que el plazo del ejecutor (`approval_seconds`, `code_seconds`), para que el ejecutor reciba siempre una respuesta.

## Herramientas

| Herramienta | Uso |
|---|---|
| `open`, `read` | Abrir una dirección (`url`) o seguir un enlace por su nombre sin modelo (`link`), y leer; con `find` para extraer solo lo que interesa |
| `task` | Un paso acotado con JEV (escribir, pulsar, rellenar), detrás de la barrera de aprobaciones |
| `login` | Entrar con el baúl; con `new_password`, cambiar la contraseña guardada |
| `attach` | Adjuntar un archivo que el agente manda en base64 o por una URL https de internet (hasta 100 MB) |
| `screenshot`, `sessions` | Captura; sesiones abiertas, y cerrar una o todas con `logout` |
| `downloads` | Las descargas guardadas en el servidor, de la más reciente a la más antigua |
| `answer` | La respuesta de Pablo, dada en el chat, a una operación parada con `NEEDS_APPROVAL` o `NEEDS_CODE` |
| `library` | Sitios y rutinas guardados; `run` ejecuta una rutina |
| `user_step` | Pedir a Pablo un paso fuera del navegador que la web detecta sola (app Cl@ve, notificación en el móvil, enlace por correo). Pablo no ve ni toca el navegador: un captcha se resuelve con una captura y su respuesta en el chat, o lo hace él desde su navegador |

`attach` solo descarga de direcciones https públicas (nada de la red de casa, Tailscale ni el propio servidor) y sin seguir redirecciones.

Las descripciones y las pautas (`INSTRUCTIONS`) vienen de lisa-agent (`src/tools/browser.ts` y `library.ts`, commit 556fd39), adaptadas a cualquier agente. El 18-10-2026 las 18 herramientas se juntaron en 11: `follow` pasó a `open` con `link`, `save_password` a `login` con `new_password`, `logout` a `sessions`, `routine` a `library` con `run`, y se quitaron `log`, `boe_search` y `official_site`.

## Descargas

Decisión de Pablo (17-10-2026): todas las descargas se quedan en el servidor, en una carpeta aparte, y él dirá después qué hacer con cada una. El ejecutor las saca de la carpeta que comparte con el contenedor del navegador y las lleva a `saved_downloads` (por defecto `/var/lib/lisa-browser/descargas`, que ninguna web alcanza), con la fecha delante del nombre y una ficha `.json` al lado (nombre original, tamaño, dirección de la que salió y hora). `downloads` las lista. No hay enlaces para bajarlas desde fuera: harían falta una ruta pública nueva, y Pablo dijo que no era necesario.

## Límites conocidos

- **Clientes sin formularios.** El 17-10-2026, Claude Code los muestra en la terminal; claude.ai y Claude Desktop no los admitían. El 20-10-2026, t3code decía admitirlos pero los rechazaba al momento sin enseñarlos (en menos de un segundo, con lo que todo envío acababa en `DENIED`); por eso un «no» que llega antes de `HUMAN_SECONDS` (2 s) se toma como cliente sin formularios, nunca como respuesta de Pablo. Si el cliente rechaza el formulario, un permiso para enviar o borrar, o un código, se pide por el chat: la operación se queda parada (el ejecutor sigue esperando, con su plazo de `approval_seconds` o `code_seconds`) y la herramienta devuelve `NEEDS_APPROVAL` (acción, campos y captura) o `NEEDS_CODE`, con el identificador de la operación. El agente pregunta a Pablo y llama a `answer`, que sigue la operación y devuelve su resultado. Mientras está parada, la cola de operaciones también espera. Pagar y una contraseña nueva terminan en `DENIED` o sin enviarse.
- **Los formularios no llevan la captura**: solo texto (acción, dominio y campos rellenos). Si Pablo quiere verla antes de aprobar, el agente puede pedir `screenshot`.
- **Los datos sensibles van en formulario normal.** La especificación de MCP prefiere el modo URL para eso, pero Claude Code no lo anunciaba bien en la versión de octubre; se usa el modo formulario, con lo que el código pasa por el cliente (nunca por el modelo ni por el registro del ejecutor).
- **Una llamada larga solo sobrevive si le llega algo cada pocos minutos.** La llamada sigue abierta hasta que la operación termina (como mucho `operation_seconds`, más lo que tarde Pablo en contestar), pero Claude Code es Node y su `fetch` (undici) corta una respuesta que pasa 300 s sin recibir nada (`bodyTimeout`, error `UND_ERR_BODY_TIMEOUT`). El SDK manda un «ping» de SSE cada 15 s, pero agentgateway no los reenvía. Así que, mientras un formulario espera respuesta, `_ask` manda cada 30 s (`KEEPALIVE_SECONDS`) «Esperando a que Pablo conteste el formulario». Lo manda como aviso de progreso, si el cliente lo pidió, y como mensaje de registro (`info`), los dos por la respuesta de la llamada. Además, otro mensaje de registro sin ligar a la llamada va por el flujo GET de la sesión, que se corta igual. Los avisos paran en cuanto Pablo contesta, rechaza o el formulario caduca. Comprobado el 17-10-2026 con un cliente Node, sin contestar a `user_step`: por `https://mcp.example.com/todo/mcp` y sin estos avisos se cortaron la llamada y el GET a los 300 s justos. Con un agentgateway 1.6.0 de prueba delante del código nuevo, los tres avisos llegaron y la llamada pasó de los 400 s. Fuera de los formularios, si la operación pasa 30 s sin novedades (en la cola detrás de otra, o un paso largo), se manda igual «El navegador sigue trabajando», así que ninguna llamada se queda callada 300 s. Si el cliente corta, la operación se cancela en el siguiente paso.
- **Un reinicio del MCP único o de este servicio corta las sesiones abiertas**: el cliente tiene que reconectarse.

## Despliegue

En el servidor `lisa`, `hosts/lisa/navegador.nix` (Python con el paquete `mcp`, y `[mcp]` en `/etc/lisa-browser.toml`) y `hosts/lisa/mcp-publico.nix` (el destino `browser` entre los `remotos`). A mano, como root: `/var/lib/lisa-secretos/mcp-unico/browser.url` con `http://127.0.0.1:8789/mcp` y `browser.token` con la clave.
