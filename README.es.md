# opencode-holographic

Dale a **OpenCode** una memoria de hechos de largo plazo, con o sin Hermes:

- **Con Hermes** (por defecto): OpenCode reutiliza **el provider real `holographic`** y **el mismo
  store SQLite** que usa Hermes, así los dos agentes recuerdan lo mismo.
- **Sin Hermes**: el provider se carga desde la copia vendorizada en este repositorio y la base vive
  **donde vos elijas** durante la instalación.

En ambos casos tenés:

- **Recall antes de cada turno**: los hechos relevantes a tu mensaje se inyectan en el contexto del
  sistema (ranking HRR + FTS5 + trust, gate de relevancia, ventana de repetición por sesión).
- **Las tools originales**: `fact_store` y `fact_feedback`, con los schemas exactos de upstream.
- **Un store local**: SQLite, sin API key, sin red, sin llamadas a modelos.

[English](README.md) · [Español](#por-qué-existe)

```
Turno de OpenCode ── hook context ──┐
                                    ├──▶ sidecar (socket Unix) ──▶ holographic.prefetch()
fact_store / fact_feedback ─────────┘                            └──▶ holographic.handle_tool_call()
                                           │
                                      memory_store.db  ◀── compartida con Hermes (WAL) o standalone
```

## Por qué existe

`holographic` es una memoria **local**: un store de hechos en SQLite con búsqueda FTS5, trust
scoring, resolución de entidades y recuperación HRR (_holographic reduced representation_).

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

Un sidecar de ~250 líneas corre el provider de verdad y lo expone por un socket Unix. El plugin de
OpenCode es un cliente fino: un hook para el recall y dos tools para escribir. Paridad exacta, y los
arreglos de upstream llegan gratis.

## Modos y dónde vive la base

| Modo           | Cuándo                               | De dónde sale el provider                               | Base de datos                                                                                                        |
| -------------- | ------------------------------------ | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| **shared**     | se detecta una instalación de Hermes | `$HERMES_HOME/plugins/holographic` (la copia de Hermes) | `config.yaml → plugins.hermes-memory-store.db_path` de Hermes                                                        |
| **standalone** | no hay Hermes                        | `vendor/holographic` + `sidecar/hermes_shim.py`         | **la elegís vos** en la instalación (`--db`, o el prompt), se guarda en `~/.config/opencode-holographic/config.json` |

`./install.sh` decide (`auto`) y **solo pregunta en modo standalone**. Podés cambiar la base cuando
quieras con `--db`, la variable `OPENCODE_HOLOGRAPHIC_DB`, o `"dbPath"` en las opciones del plugin.
En modo shared el provider lee la config de Hermes solo, así que la ruta no se duplica nunca.

Orden de resolución del store: `--db-path` → `OPENCODE_HOLOGRAPHIC_DB` → `dbPath` del archivo de
config → (shared) el `config.yaml` de Hermes → default `~/.local/share/opencode-holographic/memory_store.db`.

## Cómo funciona

| Pieza                    | Rol                                                                                                                                                                                    |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `install.sh`             | Elige el modo, pregunta dónde va la base, escribe la config, instala el servicio y lo verifica.                                                                                        |
| `sidecar/server.py`      | Corre el provider y sirve el protocolo de abajo.                                                                                                                                       |
| `sidecar/hermes_shim.py` | Suplente de los módulos de Hermes que importa el provider (`agent.memory_provider`, `hermes_cli.*`, `hermes_constants`, `hermes_state_wal`, `hermes_time`, `tools.registry`, `utils`). |
| `sidecar/discover.sh`    | Busca un intérprete con `numpy`: `$OPENCODE_HOLOGRAPHIC_PYTHON` → `.venv` del proyecto → entornos de Hermes.                                                                           |
| `vendor/holographic/`    | El provider en sí (MIT, commit fijado — ver `vendor/holographic/UPSTREAM.md`).                                                                                                         |
| `index.ts` + `src/`      | Plugin de OpenCode V2: hooks, tools, cliente del socket, config.                                                                                                                       |

**Protocolo** (JSON por líneas sobre un socket Unix, una respuesta por request):

| Comando                         | Para qué                                                                        |
| ------------------------------- | ------------------------------------------------------------------------------- |
| `ping`                          | Estado: modo, origen del provider, base, tools.                                 |
| `schemas`                       | Los schemas de las tools del provider, para que el plugin los refleje tal cual. |
| `prefetch {text, session_id}`   | El bloque de recall a inyectar antes de un turno (`""` = nada).                 |
| `tool {name, args, session_id}` | `handle_tool_call(...)` — el mismo camino que usa el provider para escribir.    |
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
- **Python 3.11+ con `numpy`**. El modo shared además necesita una instalación de Hermes (cuyo
  intérprete ya lo tiene); el modo standalone se conforma con cualquier intérprete del sistema, y
  `install.sh` te crea un virtualenv si ninguno tiene numpy.

## Instalación

Funciona en Linux, macOS y Windows. El **protocolo y los dos endpoints (socket Unix y `tcp://`)
están cubiertos por la suite de tests**; el camino del servicio en Linux es el que se ejercita de
punta a punta en cada cambio.

### Linux

```bash
git clone https://github.com/dcazabat/opencode-holographic.git ~/Documentos/opencode-holographic
cd ~/Documentos/opencode-holographic
npm install          # solo dependencias de desarrollo (types + typescript)
./install.sh         # interactivo: elige el modo y pregunta dónde va la base
```

Qué hace el instalador: detecta **shared** (hay Hermes) o **standalone**, pregunta la ruta de la
base solo en standalone, escribe `~/.config/opencode-holographic/config.json`, instala
`opencode-holographic.service` como **unidad de usuario de systemd** (`Restart=always`), la habilita
y arranca, y al final verifica e imprime las opciones del plugin.

No interactivo:

```bash
./install.sh --mode standalone --db ~/memoria/memory_store.db --yes   # sin Hermes
./install.sh --mode shared --yes                                      # reusar el store de Hermes
./install.sh --no-service --yes                                       # solo la config
./install.sh --help
```

El socket vive en `$XDG_RUNTIME_DIR/opencode-holographic.sock` (o `/tmp/opencode-holographic-<uid>.sock`
si no hay `XDG_RUNTIME_DIR`). En una máquina **sin systemd** (contenedores, distros mínimas) usá
`--no-service` y supervisá el proceso vos:

```bash
sidecar/run.sh --verbose            # primer plano
nohup sidecar/run.sh &              # o bajo s6/supervisord/runit
```

Con `"spawn": true` (default) OpenCode además arranca el sidecar bajo demanda si falta el socket,
así que el servicio es una comodidad y no un requisito.

### macOS

Mismos comandos; el instalador detecta Darwin e instala un **LaunchAgent** en lugar del unit systemd:

```bash
./install.sh
launchctl list | grep holographic
tail -f ~/Library/Logs/opencode-holographic.log
```

- Archivo del servicio: `~/Library/LaunchAgents/com.opencode.holographic.plist` (plantilla en `launchd/`).
- Logs: `~/Library/Logs/opencode-holographic.log`.
- El socket vive en `~/Library/Caches/opencode-holographic/sidecar.sock`, porque macOS no tiene
  `XDG_RUNTIME_DIR` (y `/tmp` es escribible por todos).
- Para detenerlo: `launchctl unload -w ~/Library/LaunchAgents/com.opencode.holographic.plist`;
  para arrancarlo de nuevo: `launchctl load -w …`.
- Si macOS bloquea los scripts (`Operation not permitted`): `chmod +x sidecar/*.sh install.sh`.

### Windows

Windows usa un **endpoint TCP en loopback** en lugar del socket Unix: ahí Node no puede conectarse
a un socket `AF_UNIX` de Python (usa named pipes), así que el default es `tcp://127.0.0.1:4748`.

```powershell
git clone https://github.com/dcazabat/opencode-holographic.git "$HOME\Documentos\opencode-holographic"
cd "$HOME\Documentos\opencode-holographic"

py -3 -m venv .venv
.venv\Scripts\python -m pip install numpy          # numpy es todo lo que necesita el modo standalone

# verificar la resolución (modo, provider, base, tools)
.venv\Scripts\python sidecar\server.py --check --mode standalone --db "$HOME\memoria\memory_store.db"
```

Escribí la config (`%USERPROFILE%\.config\opencode-holographic\config.json`):

```json
{
  "mode": "standalone",
  "dbPath": "C:\\Users\\vos\\memoria\\memory_store.db",
  "socketPath": "tcp://127.0.0.1:4748",
  "hermesHome": "C:\\Users\\vos\\.hermes"
}
```

Arrancalo al iniciar sesión con el **Programador de tareas** (el equivalente al unit systemd):

```powershell
# una tarea que arranca el sidecar al iniciar sesión y lo reinicia si se cae
schtasks /create /tn "opencode-holographic" /sc onlogon /rl limited ^
  /tr "\"$HOME\Documentos\opencode-holographic\sidecar\run.cmd\""
schtasks /run /tn "opencode-holographic"
```

`sidecar\run.cmd` elige el intérprete (`%OPENCODE_HOLOGRAPHIC_PYTHON%` → `.venv` → `python`), setea
`PYTHONPATH` y arranca el server. Usá `run.cmd --verbose` para verlo, o poné un acceso directo en
`shell:startup` en lugar del Programador de tareas.

Entrada del plugin en `%USERPROFILE%\.config\opencode\opencode.json`:

```jsonc
{
  "plugins": [
    {
      "package": "C:\\Users\\vos\\Documentos\\opencode-holographic",
      "options": {
        "socketPath": "tcp://127.0.0.1:4748",
        "configPath": "C:\\Users\\vos\\.config\\opencode-holographic\\config.json",
      },
    },
  ],
}
```

Notas para Windows:

- El puerto se ata solo a `127.0.0.1`. Si lo atás a un host no-loopback el sidecar avisa en el log;
  seteá `"token"` en la config (o `OPENCODE_HOLOGRAPHIC_TOKEN`) — cada request tiene que mandarlo.
- Hermes en Windows: si lo tenés, `"mode": "shared"` carga el provider desde
  `%HERMES_HOME%\plugins\holographic` y la ruta del store desde su `config.yaml`.
- Se requiere `.venv\Scripts\python.exe`: el lanzador lo busca, y si no está usa `python` del `PATH`.

### WSL y contenedores

- **WSL**: seguí los pasos de Linux dentro de la distribución. Si OpenCode corre en Windows,
  necesitás el endpoint TCP para llegar a un sidecar del lado WSL.
- **Docker / devcontainers**: el sidecar tiene que correr donde corre OpenCode, o exponer el
  endpoint. Un socket Unix necesita un montaje compartido; sin él, corré el sidecar con
  `--socket tcp://0.0.0.0:4748 --token <secreto>` **dentro del mismo namespace de red** y apuntá el
  plugin a `tcp://host:4748` con el mismo token.

### Verificar

```bash
opencode service restart                  # después de cambiar opencode.json
sidecar/run.sh --check                    # modo, provider, base, tools, endpoint
```

`--check` imprime algo así:

```json
{
  "ok": true,
  "mode": "standalone",
  "provider": "vendored",
  "db_path": "/home/vos/memoria/memory_store.db",
  "tools": ["fact_feedback", "fact_store"],
  "endpoint": "/run/user/1000/opencode-holographic.sock"
}
```

### Instalación manual (sin el instalador)

```bash
npm test                                   # 25 tests, siempre contra una copia del store
cp systemd/opencode-holographic.service ~/.config/systemd/user/   # Linux: ajustá las rutas adentro
cp launchd/com.opencode.holographic.plist ~/Library/LaunchAgents/  # macOS: igual
systemctl --user daemon-reload && systemctl --user enable --now opencode-holographic
```

## Configuración

`~/.config/opencode-holographic/config.json` (lo escribe el instalador):

| Clave                                                                                                                | Significado                                                  |
| -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `mode`                                                                                                               | `shared` \| `standalone` (`auto` por defecto en el sidecar). |
| `dbPath`                                                                                                             | El archivo del store (standalone).                           |
| `socketPath`                                                                                                         | Dónde escucha el sidecar.                                    |
| `hermesHome`                                                                                                         | Instalación de Hermes a usar en modo shared.                 |
| `hrr_dim`, `hrr_weight`, `prefetch_min_coverage`, `prefetch_repeat_turns`, `min_trust_threshold`, `recall_source`, … | Se pasan directo al provider.                                |

Opciones del plugin en `opencode.json` (todas opcionales; las variables de entorno funcionan como
defaults):

| Opción       | Variable                      | Default                                                     | Significado                                                                                        |
| ------------ | ----------------------------- | ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `socketPath` | `OPENCODE_HOLOGRAPHIC_SOCKET` | socket unix (Linux/macOS), `tcp://127.0.0.1:4748` (Windows) | Endpoint: una ruta, `unix:/ruta`, o `tcp://host:puerto`.                                           |
| `token`      | `OPENCODE_HOLOGRAPHIC_TOKEN`  | –                                                           | Secreto compartido; cada request tiene que mandarlo (recomendado para TCP en un host no-loopback). |
| `configPath` | `OPENCODE_HOLOGRAPHIC_CONFIG` | `~/.config/opencode-holographic/config.json`                | Archivo de config que recibe el sidecar.                                                           |
| `dbPath`     | `OPENCODE_HOLOGRAPHIC_DB`     | –                                                           | Ruta explícita del store (pisa el archivo de config).                                              |
| `launcher`   | –                             | `<proyecto>/sidecar/run.sh` (`run.cmd` en Windows)          | Arranca el sidecar si el socket no está.                                                           |
| `pythonPath` | `OPENCODE_HOLOGRAPHIC_PYTHON` | auto-detectado                                              | Override del intérprete.                                                                           |
| `hermesHome` | `HERMES_HOME`                 | `~/.hermes`                                                 | Home de Hermes (modo shared).                                                                      |
| `timeoutMs`  | –                             | `8000`                                                      | Timeout por request; si falla, degrada a "sin memoria".                                            |
| `spawn`      | –                             | `true`                                                      | Arranca el sidecar bajo demanda si el servicio está caído.                                         |
| `debug`      | –                             | `false`                                                     | Loguea el tráfico del socket en el log de OpenCode.                                                |

## Operación

```bash
systemctl --user status opencode-holographic.service
journalctl --user -u opencode-holographic -f      # modo, recall, filtros de escritura y errores
systemctl --user restart opencode-holographic     # después de actualizar Hermes
sidecar/run.sh --check --verbose                  # qué haría el sidecar, sin arrancarlo
npm run sidecar                                   # correr el sidecar en primer plano
```

## Problemas frecuentes

| Síntoma                                              | Causa / solución                                                                                                                                                 |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `configured plugin path must be a directory`         | Apuntá `plugins[].package` al directorio del proyecto, no a `dist/index.js`.                                                                                     |
| Las tools no aparecen en la sesión                   | Reiniciá OpenCode (`opencode service restart`) tras cambiar `opencode.json`.                                                                                     |
| `no database path configured; using the default …`   | Standalone sin nada configurado: corré `./install.sh` (escribe `dbPath`) o pasá `--db`.                                                                          |
| `standalone mode needs the vendored provider at …`   | El clone está incompleto: debe existir `vendor/holographic/`.                                                                                                    |
| `discover.sh: no interpreter with numpy found`       | Corré `./install.sh` (crea el `.venv`) o seteá `OPENCODE_HOLOGRAPHIC_PYTHON`.                                                                                    |
| Windows: `run.cmd` no hace nada                      | Usá `.venv\Scripts\python.exe` (el lanzador lo busca, y si no usa `python` del `PATH`) y mirá el historial del Programador de tareas.                            |
| Windows: connection refused en `127.0.0.1:4748`      | El sidecar no está corriendo, o el puerto lo tiene otro proceso; corré `sidecar\run.cmd --verbose` y cambiá el puerto en la config y en las opciones del plugin. |
| macOS: `Operation not permitted` al correr un script | `chmod +x sidecar/*.sh install.sh`.                                                                                                                              |
| macOS: el socket no está en `/tmp`                   | Es esperado: vive en `~/Library/Caches/opencode-holographic/` (no hay `XDG_RUNTIME_DIR`).                                                                        |
| `unauthorized` desde el sidecar                      | Hay un token configurado; poné el mismo `"token"` en las opciones del plugin o en `OPENCODE_HOLOGRAPHIC_TOKEN`.                                                  |
| El recall no encuentra nada                          | Verificá la base con `fact_store({action:"stats"})`; los saludos y mensajes cortos no recuerdan nada a propósito.                                                |
| Los tests saltan con _"requires a Hermes install"_   | Es lo esperado sin Hermes. Con Hermes pero sin hechos, seteá `HOLOGRAPHIC_TEST_SOURCE` a un `memory_store.db`.                                                   |

**Inspeccionar el store a mano:** copiá la base **con su `-wal` y su `-shm`** antes de leerla. El
archivo `.db` puede estar muy atrás del estado vivo (vectores, tablas nuevas) mientras los cambios
pendientes están en el WAL; y la ruta _default_ de Hermes puede existir como un señuelo vacío de
0 bytes mientras el store real es el `db_path` configurado.

## Limitaciones

- Un solo usuario: el provider corre en scope `profile` (los scopes por usuario / por chat no se usan).
- Las escrituras de `fact_store` no se reflejan en `MEMORY.md` / `USER.md` de Hermes; solo se comparte el store de hechos.
- Los hechos que parecen instrucciones o credenciales quedan fuera del **recall** (por diseño); `search` igual los encuentra.
- El modo standalone sigue el commit vendorizado; ver `vendor/holographic/UPSTREAM.md` para actualizarlo.
- El modo shared depende del layout interno de Hermes. `discover.sh` re-detecta el intérprete, pero reiniciá el servicio después de actualizar Hermes.
- Sin afiliación ni respaldo de Nous Research ni del proyecto OpenCode.

## Upstream vendorizado

`vendor/holographic/` es una copia literal del provider con licencia MIT
([`itpartypattaya/hermes-plugin-holographic`](https://github.com/itpartypattaya/hermes-plugin-holographic),
originalmente `NousResearch/hermes-plugin-holographic`), fijada por commit en
`vendor/holographic/UPSTREAM.md`. Su `LICENSE` y `LICENSES/` (datos de diccionarios) se conservan.
Solo se usa en modo standalone; cuando Hermes está presente, gana su propia copia.
