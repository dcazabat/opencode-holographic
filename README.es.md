# opencode-holographic

Dale a **OpenCode** la misma memoria de largo plazo que tiene **Hermes** — reutilizando el
provider real `holographic` de Hermes: el mismo código, el mismo store SQLite, sin reimplementar nada.

- **Recall antes de cada turno**: los hechos relevantes a tu mensaje se inyectan en el contexto del
  sistema (ranking HRR + FTS5 + trust, con gate de relevancia y ventana de repetición por sesión).
- **Las tools originales**: `fact_store` y `fact_feedback`, con los schemas exactos que trae Hermes.
- **Un store compartido**: OpenCode y Hermes leen y escriben el mismo `memory_store.db` (WAL), así
  que un hecho aprendido en uno aparece en el otro.

[English](README.md) · [Español](#por-qué-existe)

```
Turno de OpenCode ── hook context ──┐
                                    ├──▶ sidecar (socket Unix) ──▶ holographic.prefetch()
fact_store / fact_feedback ─────────┘                            └──▶ holographic.handle_tool_call()
                                           │
                                      memory_store.db  ◀── compartida con Hermes (WAL)
```

## Por qué existe

El provider `holographic` de Hermes es una memoria **local**: un store de hechos en SQLite con
búsqueda FTS5, trust scoring, resolución de entidades y recuperación HRR (_holographic reduced
representation_). Sin API key, sin red, sin llamadas a modelos.

OpenCode no tiene un provider así, y hay tres caminos:

1. **Usar otro plugin de memoria** (opencode-mem, Hindsight, Mem0, …). Buenos productos, pero
   mantienen _su propio_ store: nada de lo que le enseñaste a Hermes se recuerda en OpenCode, ni al
   revés.
2. **Reimplementar `holographic` en TypeScript.** Son ~5.700 líneas de Python (`text.py` solo tiene
   2.031: stopwords, formas, diccionarios CJK). Un port se desincroniza de upstream en cada release.
   El port hermano que ya existe
   ([`tw2818/holographic-opencode-plugin`](https://github.com/tw2818/holographic-opencode-plugin))
   lo demuestra: mismo DDL de tablas, pero **vectores float64 crudos sin el prefijo `HRR1`**,
   **fusión RRF** en lugar del weighted `0.4 FTS5 + 0.3 jaccard + 0.3 HRR`, sin gate IDF de
   relevancia, sin ventana de repetición por sesión, y apunta a la API **V1** de OpenCode. No puede
   leer un store de Hermes.
3. **Reutilizar el provider real detrás de una frontera chica** ← _este proyecto_.

Un sidecar de ~150 líneas corre el provider `holographic` de verdad, dentro del entorno aislado de
Python de Hermes, y lo expone por un socket Unix. El plugin de OpenCode es un cliente fino: un hook
para el recall y dos tools para escribir. Paridad exacta, y los arreglos de upstream llegan gratis.

## Cómo funciona

| Pieza                 | Rol                                                                                                 |
| --------------------- | --------------------------------------------------------------------------------------------------- |
| `sidecar/server.py`   | Corre el provider real (`HolographicMemoryProvider`) y sirve el protocolo de abajo.                 |
| `sidecar/discover.sh` | Elige el entorno de Hermes más nuevo cuyo intérprete importe `numpy`, `ruamel.yaml` y `hermes_cli`. |
| `sidecar/run.sh`      | Lanzador: setea `PYTHONPATH=<hermes home>/plugins` y ejecuta el server.                             |
| `index.ts` + `src/`   | Plugin de OpenCode V2: hooks, tools, cliente del socket, config.                                    |
| `systemd/…service`    | Sidecar siempre vivo (servicio de usuario, `Restart=always`).                                       |

**Protocolo** (JSON por líneas sobre un socket Unix, una respuesta por request):

| Comando                         | Para qué                                                                        |
| ------------------------------- | ------------------------------------------------------------------------------- |
| `ping`                          | Estado del sidecar: ruta de la base, tools, sesión actual.                      |
| `schemas`                       | Los schemas de las tools del provider, para que el plugin los refleje tal cual. |
| `prefetch {text, session_id}`   | El bloque de recall a inyectar antes de un turno (`""` = nada).                 |
| `tool {name, args, session_id}` | `handle_tool_call(...)` — el mismo camino que usa Hermes para escribir.         |
| `status`                        | Indicador de recall del último prefetch.                                        |
| `shutdown`                      | Detiene el sidecar.                                                             |

**Del lado de OpenCode:**

- `ctx.session.hook("context")` → extrae el último mensaje del usuario, pide el recall y lo empuja en
  `event.system` como datos.
- `ctx.session.hook("compaction")` → reinjecta el mismo bloque para que los hechos sobrevivan al
  reescribir el transcript.
- `ctx.tool.transform(...)` → registra `fact_store` / `fact_feedback`, ejecutadas a través del sidecar.

Dos detalles que importan:

- OpenCode vuelve a correr el hook `context` en **cada llamada al modelo** (incluidas las
  continuaciones por tool). El plugin calcula el recall **una vez por mensaje de usuario** y reusa el
  bloque, así la ventana de repetición avanza una vez por turno (como Hermes) mientras los hechos
  siguen presentes durante todo el turno.
- Las escrituras pasan por `handle_tool_call`, así que los filtros de escritura (los hechos que
  parecen instrucciones o credenciales quedan fuera del recall), el vector `HRR1` en float32 y la
  fila de historial en `fact_versions` los produce el propio provider. El plugin nunca hace SQL crudo
  ni migra el esquema.

## Requisitos

- **OpenCode V2**.
- Una instalación de **Hermes** con el plugin `holographic` habilitado, `memory.provider: holographic`
  y un `plugins.hermes-memory-store.db_path` apuntando a un `memory_store.db`.
  El sidecar resuelve la ruta desde `~/.hermes/config.yaml`, así que no hay nada que duplicar.
- Python 3.11+ con `numpy` (el entorno que trae Hermes ya lo tiene).

## Instalación

```bash
# 1. fuentes
git clone https://github.com/dcazabat/opencode-holographic.git ~/Documentos/opencode-holographic
cd ~/Documentos/opencode-holographic
npm install          # solo dependencias de desarrollo (types + typescript)
npm run build        # opcional: compila dist/ (fallback y usado por los tests)
npm test             # 20 tests; copian el store y nunca tocan el real

# 2. el sidecar como servicio de usuario (ajustá la ruta del unit si clonaste en otro lado)
cp systemd/opencode-holographic.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now opencode-holographic.service
systemctl --user status opencode-holographic.service
```

Registrá el plugin en `~/.config/opencode/opencode.json`. La ruta debe ser un **directorio**
(OpenCode rechaza una ruta a archivo con _"configured plugin path must be a directory"_), y OpenCode
carga `<directorio>/index.ts`, así que editar el TypeScript **no requiere rebuild**:

```jsonc
{
  "plugins": [
    {
      "package": "/home/<vos>/Documentos/opencode-holographic",
      "options": {
        "socketPath": "/run/user/1000/opencode-holographic.sock",
        "timeoutMs": 8000,
        "spawn": true,
        "debug": false,
      },
    },
  ],
}
```

Después reiniciá el servicio y verificá:

```bash
opencode service restart
python3 - <<'EOF'
import socket, json, os
path = f"{os.environ['XDG_RUNTIME_DIR']}/opencode-holographic.sock"
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(path)
f = s.makefile("rwb")
f.write((json.dumps({"id": 1, "cmd": "ping", "args": {}}) + "\n").encode()); f.flush()
print(json.loads(f.readline())["result"])
EOF
```

En una sesión, las tools ya deberían estar disponibles:

```
fact_store({ action: "stats" })
fact_store({ action: "search", query: "deploy" })
fact_store({ action: "add", content: "Algo que valga recordar." })
```

## Configuración

Las opciones van en la entrada del plugin en `opencode.json`; las variables de entorno funcionan como
defaults.

| Opción       | Variable                      | Default                                      | Significado                                                |
| ------------ | ----------------------------- | -------------------------------------------- | ---------------------------------------------------------- |
| `socketPath` | `OPENCODE_HOLOGRAPHIC_SOCKET` | `$XDG_RUNTIME_DIR/opencode-holographic.sock` | Socket donde escucha el sidecar.                           |
| `launcher`   | –                             | `<proyecto>/sidecar/run.sh`                  | Arranca el sidecar si el socket no está.                   |
| `pythonPath` | `OPENCODE_HOLOGRAPHIC_PYTHON` | auto-detectado                               | Sobrescribe `sidecar/discover.sh`.                         |
| `hermesHome` | `HERMES_HOME`                 | `~/.hermes`                                  | Donde viven el paquete `holographic` y su config.          |
| `dbPath`     | `OPENCODE_HOLOGRAPHIC_DB`     | del config de Hermes                         | Ruta explícita del store.                                  |
| `timeoutMs`  | –                             | `8000`                                       | Timeout por request; si falla, degrada a "sin memoria".    |
| `spawn`      | –                             | `true`                                       | Arranca el sidecar bajo demanda si el servicio está caído. |
| `debug`      | –                             | `false`                                      | Loguea el tráfico del socket en el log de OpenCode.        |

## Operación

```bash
systemctl --user status opencode-holographic.service
journalctl --user -u opencode-holographic -f      # recall, filtros de escritura y errores
systemctl --user restart opencode-holographic     # después de actualizar Hermes
npm run sidecar                                   # correr el sidecar en primer plano (verbose)
```

## Problemas frecuentes

| Síntoma                                                  | Causa / solución                                                                                                    |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `configured plugin path must be a directory`             | Apuntá `plugins[].package` al directorio del proyecto, no a `dist/index.js`.                                        |
| Las tools no aparecen en la sesión                       | Reiniciá OpenCode (`opencode service restart`) tras cambiar `opencode.json`.                                        |
| `sidecar not reachable at setup, using fallback schemas` | El servicio está caído; el cliente lo arranca si `spawn: true`.                                                     |
| `discover.sh: no Hermes interpreter found`               | Ningún entorno importa `numpy` + `ruamel.yaml` + `hermes_cli`; seteá `OPENCODE_HOLOGRAPHIC_PYTHON`.                 |
| El recall no encuentra nada                              | Verificá `db_path` con `fact_store({action:"stats"})`; los saludos y mensajes cortos no recuerdan nada a propósito. |
| Los tests saltan con _"requires a Hermes install"_       | Es lo esperado sin Hermes. Con Hermes pero sin hechos, seteá `HOLOGRAPHIC_TEST_SOURCE` a un `memory_store.db`.      |

**Inspeccionar el store a mano:** copiá la base **con su `-wal` y su `-shm`** antes de leerla. El
archivo `.db` puede estar muy atrás del estado vivo (vectores, tablas nuevas) mientras los cambios
pendientes están en el WAL; y la ruta _default_ de Hermes puede existir como un señuelo vacío de
0 bytes mientras el store real es el `db_path` configurado.

## Limitaciones

- Un solo usuario: el provider corre en scope `profile` (los scopes por usuario / por chat de Hermes no se usan).
- Las escrituras de `fact_store` no se reflejan en `MEMORY.md` / `USER.md` de Hermes; solo se comparte el store de hechos.
- Los hechos que parecen instrucciones o credenciales quedan fuera del **recall** (por diseño); `search` igual los encuentra.
- Depende del layout interno de Hermes. `discover.sh` re-detecta el intérprete solo, pero después de actualizar Hermes reiniciá el servicio.
- Sin afiliación ni respaldo de Nous Research ni del proyecto OpenCode.
