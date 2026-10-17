# MCP del navegador

Desde el 17-10-2026 el navegador se usa como un MCP: el destino `browser` del MCP único de Pablo (`https://mcp.example.com/todo/mcp`). Así lo maneja cualquier agente que Pablo conecte (Claude Code, claude.ai, Codex…), y no solo el chat de Lisa, que se retiró ese día junto con lisa-agent.

## Decisiones de Pablo (17-10-2026)

- **Lo que hay que preguntarle se pregunta en la ventana de su agente**, con un formulario MCP (*elicitation*): el permiso para enviar, borrar o pagar, el código de un segundo factor, la tarjeta y una contraseña nueva. Lo que escribe en el formulario va del cliente al ejecutor; el modelo no lo ve y no puede contestar por él. Pablo prefirió esto a un aviso en el móvil o a una web con passkey, sabiendo que no todos los clientes admiten formularios y que los códigos pasan por el cliente.
- **El agente ve las capturas**: `screenshot` devuelve la imagen. Antes iba solo al chat, no al modelo.
- **Entra todo lo que ya existía**: navegar, leer, pasos con JEV, inicio de sesión con el baúl, rutinas, archivos y trámites (certificado digital, tasas con tarjeta, BOE y sedes oficiales).

## Cómo funciona

```text
agente ──► mcp.example.com (Caddy, puerta con clave) ──► agentgateway (MCP único)
                                                          └─► 127.0.0.1:8789/mcp (este servicio, con su clave)
                                                                └─ mismo proceso que la API de 127.0.0.1:8787
```

- `mcp_server.py` es un servidor MCP (SDK oficial de Python, Streamable HTTP) que corre en el mismo proceso que la API HTTP. Cada herramienta crea una operación del ejecutor, como la API: misma cola (una operación a la vez), misma barrera de aprobaciones y mismo registro.
- Se sirve solo si la configuración trae `[mcp]` (ver `config.example.toml`). Exige `Authorization: Bearer <clave>`, que pone el MCP único (su `browser.token`); por defecto es la misma clave que la API.
- agentgateway antepone `browser_` a cada herramienta: `open` se ve como `browser_open`. Se comprobó el 17-10-2026 que agentgateway 1.6.0 deja pasar los formularios entre el servidor y el cliente, también con varios destinos juntos.

### Eventos del ejecutor

| Evento | Qué hace el MCP |
|---|---|
| `step`, `queued` | Aviso de progreso al cliente, si lo pidió |
| `approval` | Formulario «¿Lo permites?». Con enviar, además «permitir siempre en este sitio» (regla `navegador:enviar:<dominio>` en `reglas.json`; nunca para borrar ni dinero). No contestar, rechazar o dejarlo sin marcar es `deny`. Si el cliente no admite formularios, también |
| `secret` | Formulario con el código, la tarjeta (número, caducidad y CVV) o la contraseña nueva. Si no contesta, no se envía nada y el ejecutor lo da por caducado |
| `screenshot` | La imagen va en el resultado |
| `done` | El resultado, en JSON compacto y sin `metrics` (siguen en `log`). Las descargas de hasta 10 MB van dentro como recurso; las mayores se quedan en el servidor |

Los formularios caducan 15 s antes que el plazo del ejecutor (`approval_seconds`, `code_seconds`), para que el ejecutor reciba siempre una respuesta.

## Herramientas

| Herramienta | Uso |
|---|---|
| `open`, `follow`, `read` | Abrir, seguir un enlace sin modelo y leer, con `find` para extraer solo lo que interesa |
| `task` | Un paso acotado con JEV (escribir, pulsar, rellenar), detrás de la barrera de aprobaciones |
| `login`, `save_password` | Entrar con el baúl o el certificado digital; cambiar la contraseña guardada |
| `card` | Pagar una tasa con la tarjeta que Pablo escribe en el formulario |
| `attach` | Adjuntar un archivo que el agente manda en base64 o por una URL https de internet (hasta 100 MB) |
| `screenshot`, `sessions`, `logout`, `log` | Captura, sesiones abiertas, cerrar sesión y registro |
| `routine`, `library` | Rutinas y sitios guardados |
| `boe_search`, `official_site`, `user_step` | Trámites: buscar la norma en el BOE, comprobar que una sede es oficial y pedir a Pablo un paso a mano (Cl@ve, app del banco, captcha) |

`attach` solo descarga de direcciones https públicas (nada de la red de casa, Tailscale ni el propio servidor) y sin seguir redirecciones.

Las descripciones y las pautas (`INSTRUCTIONS`) vienen de lisa-agent (`src/tools/browser.ts`, `library.ts` y `tramites.ts`, commit 556fd39), adaptadas a cualquier agente. `tramites.py` porta su lista de 212 sedes oficiales y su cliente del BOE, que a su vez venían de SpainAssist.

## Límites conocidos

- **Clientes sin formularios.** El 17-10-2026, Claude Code los muestra en la terminal; claude.ai y Claude Desktop no los admiten todavía. Con ellos se puede leer y navegar, pero todo lo que pida permiso o un código termina en `DENIED`, sin hacerse.
- **Los formularios no llevan la captura**: solo texto (acción, dominio y campos rellenos). Si Pablo quiere verla antes de aprobar, el agente puede pedir `screenshot`.
- **Los datos sensibles van en formulario normal.** La especificación de MCP prefiere el modo URL para eso, pero Claude Code no lo anunciaba bien en la versión de octubre; se usa el modo formulario, con lo que el código o la tarjeta pasan por el cliente (nunca por el modelo ni por el registro del ejecutor).
- **Una operación larga mantiene abierta la llamada** hasta que termina (como mucho `operation_seconds`, más lo que tarde Pablo en contestar). Si el cliente corta, la operación se cancela en el siguiente paso.
- **Un reinicio del MCP único o de este servicio corta las sesiones abiertas**: el cliente tiene que reconectarse.

## Despliegue

En el servidor `lisa`, `hosts/lisa/navegador.nix` (Python con el paquete `mcp`, y `[mcp]` en `/etc/lisa-browser.toml`) y `hosts/lisa/mcp-publico.nix` (el destino `browser` entre los `remotos`). A mano, como root: `/var/lib/lisa-secretos/mcp-unico/browser.url` con `http://127.0.0.1:8789/mcp` y `browser.token` con la clave.
