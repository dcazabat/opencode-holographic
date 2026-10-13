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

```bash
git clone https://github.com/dcazabat/opencode-holographic.git ~/Documentos/opencode-holographic
cd ~/Documentos/opencode-holographic
npm install            # solo dependencias de desarrollo (types + typescript)
./install.sh           # interactivo: elige el modo y pregunta dónde va la base
```

Ejemplos no interactivos:

```bash
./install.sh --mode standalone --db ~/memoria/memory_store.db --yes   # sin Hermes
./install.sh --mode shared --yes                                      # reusar el store de Hermes
./install.sh --help
```

El instalador escribe `~/.config/opencode-holographic/config.json`, instala el servicio **de usuario**
`opencode-holographic.service` (`Restart=always`), lo arranca e imprime un resumen:

```
==> mode: standalone
==> database: /home/vos/memoria/memory_store.db (will be created)
  mode=standalone  provider=vendored  db=/home/vos/memoria/memory_store.db  tools=fact_feedback,fact_store
```

Después registrá el plugin en `~/.config/opencode/opencode.json`. La ruta debe ser un **directorio**
(OpenCode rechaza una ruta a archivo con _"configured plugin path must be a directory"_), y OpenCode
carga `<directorio>/index.ts`, así que editar el TypeScript **no requiere rebuild**:

```jsonc
{
  "plugins": [
    {
      "package": "/home/<vos>/Documentos/opencode-holographic",
      "options": { "socketPath": "/run/user/1000/opencode-holographic.sock" },
    },
  ],
}
```

Reiniciá OpenCode y verificá:

```bash
opencode service restart
~/Documentos/opencode-holographic/sidecar/run.sh --check   # modo, provider, base, tools
```

En una sesión, las tools ya deberían estar disponibles:

```
fact_store({ action: "stats" })
fact_store({ action: "search", query: "deploy" })
fact_store({ action: "add", content: "Algo que valga recordar." })
```

### Instalación manual (sin el instalador)

```bash
npm test                                   # 24 tests, siempre contra una copia del store
cp systemd/opencode-holographic.service ~/.config/systemd/user/   # ajustá las rutas adentro
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

| Opción       | Variable                      | Default                                      | Significado                                                |
| ------------ | ----------------------------- | -------------------------------------------- | ---------------------------------------------------------- |
| `socketPath` | `OPENCODE_HOLOGRAPHIC_SOCKET` | `$XDG_RUNTIME_DIR/opencode-holographic.sock` | Socket donde escucha el sidecar.                           |
| `configPath` | `OPENCODE_HOLOGRAPHIC_CONFIG` | `~/.config/opencode-holographic/config.json` | Archivo de config que recibe el sidecar.                   |
| `dbPath`     | `OPENCODE_HOLOGRAPHIC_DB`     | –                                            | Ruta explícita del store (pisa el archivo de config).      |
| `launcher`   | –                             | `<proyecto>/sidecar/run.sh`                  | Arranca el sidecar si el socket no está.                   |
| `pythonPath` | `OPENCODE_HOLOGRAPHIC_PYTHON` | auto-detectado                               | Override del intérprete.                                   |
| `hermesHome` | `HERMES_HOME`                 | `~/.hermes`                                  | Home de Hermes (modo shared).                              |
| `timeoutMs`  | –                             | `8000`                                       | Timeout por request; si falla, degrada a "sin memoria".    |
| `spawn`      | –                             | `true`                                       | Arranca el sidecar bajo demanda si el servicio está caído. |
| `debug`      | –                             | `false`                                      | Loguea el tráfico del socket en el log de OpenCode.        |

## Operación

```bash
systemctl --user status opencode-holographic.service
journalctl --user -u opencode-holographic -f      # modo, recall, filtros de escritura y errores
systemctl --user restart opencode-holographic     # después de actualizar Hermes
sidecar/run.sh --check --verbose                  # qué haría el sidecar, sin arrancarlo
npm run sidecar                                   # correr el sidecar en primer plano
```

## Problemas frecuentes

| Síntoma                                            | Causa / solución                                                                                                  |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `configured plugin path must be a directory`       | Apuntá `plugins[].package` al directorio del proyecto, no a `dist/index.js`.                                      |
| Las tools no aparecen en la sesión                 | Reiniciá OpenCode (`opencode service restart`) tras cambiar `opencode.json`.                                      |
| `no database path configured; using the default …` | Standalone sin nada configurado: corré `./install.sh` (escribe `dbPath`) o pasá `--db`.                           |
| `standalone mode needs the vendored provider at …` | El clone está incompleto: debe existir `vendor/holographic/`.                                                     |
| `discover.sh: no interpreter with numpy found`     | Corré `./install.sh` (crea el `.venv`) o seteá `OPENCODE_HOLOGRAPHIC_PYTHON`.                                     |
| El recall no encuentra nada                        | Verificá la base con `fact_store({action:"stats"})`; los saludos y mensajes cortos no recuerdan nada a propósito. |
| Los tests saltan con _"requires a Hermes install"_ | Es lo esperado sin Hermes. Con Hermes pero sin hechos, seteá `HOLOGRAPHIC_TEST_SOURCE` a un `memory_store.db`.    |

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
