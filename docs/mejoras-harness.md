# Mejoras del harness — guía vigente

## Qué cambió

- PermissionGate usa la decisión de Config: read-only prevalece sobre allow, la aprobación puntual funciona y los permisos permanentes pertenecen a la instancia.
- Las rutas resuelven symlinks; glob rechaza escapes y filtra resultados; grep no abre ficheros externos. Esto no es un sandbox ni elimina carreras check/use.
- Todas las reglas shell allow requieren aprobación: ni un prefijo ni una regex pueden probar sus efectos. Las reglas deny se mantienen; --yes sigue siendo una autorización explícita de sesión de confianza.
- `ToolResult` separa éxito, texto, truncado y artefactos, conservando desempaquetado e índices de la antigua tupla. Las trazas correlacionan llamadas/resultados y no presentan intentos fallidos como escrituras exitosas.
- Estados de tarea y códigos de salida explícitos. Plan marcado done o herramientas exitosas no equivalen a verificación semántica.
- Presupuesto compartido de tiempo/llamadas/tokens, incluidos subagentes. El límite de llamadas cuenta inferencia y herramientas. Los tokens se contabilizan tras cada respuesta; el proveedor puede consumir más de lo estimado en la llamada en curso.
- Compresión incluye argumentos y esquemas, conserva la tarea original y descarta grupos completos de acciones/observaciones. La estimación sigue siendo por caracteres: no garantiza el límite exacto del tokenizer.
- Lectura incremental, glob incremental, límite de resultados y procesos con salida acotada. Exceso de salida detiene el proceso y devuelve error, no éxito truncado.
- Memoria acotada que conserva el final reciente; fuentes con caché invalidada por mtime/tamaño y reescritura atómica.
- CLI init puede reparar TOML malformado y respeta --rules. El instalador conserva reglas al rechazar sobrescritura. No se permite ignorar reglas cuando falta tomllib.
- Cada encargo interactivo usa una traza independiente. IDs únicos y redacción de contenido de escrituras/resultados; comandos, rutas y tarea pueden contener datos sensibles: revisa trazas antes de compartirlas.
- Shell configurado usa argv explícito: bash -c, cmd /c, PowerShell -Command. Cerebro usa el Python activo.
- Fetch bloquea destinos privados por defecto y comprueba redirecciones y peticiones del render. Puede habilitarse con `[network] allow_private = true`; el endpoint Ollama no pasa por esa política.

## Arquitectura física

```text
harness/
  core/runtime.py          presupuesto y verificación
  providers/llm.py         Ollama, dialectos y métricas
  policy/permissions.py    aprobación
  policy/network.py        destinos de red
  tools/                   registro, handlers, procesos, resultados, esquema
  skills/                  cargador y tres skills locales empaquetadas
  mcp/                     cliente stdio opcional
  storage/                 memoria, fuentes, trazas
  loop.py                  orquestación (conservada para evitar una migración masiva)
  config.py                configuración
```

Los imports históricos `harness.llm`, `harness.memory`, etc. conservan identidad mediante alias. No hay dependencias runtime nuevas.

## Uso y verificación

```bash
loagen --init
loagen --list-skills
loagen --skill code-review "revisa el código"
loagen --yes --expect-content 'notas.txt=hola' 'escribe en notas.txt el texto hola'
loagen --expect-file informe.md 'crea informe.md'
```

`--expect-content PATH=TEXT` comprueba igualdad exacta, incluidos saltos de línea. `--expect-file` comprueba que pueda leerse un fichero dentro del workspace. No prueba su corrección funcional ni que sea nuevo. Contratos grandes (>1 MB) no se aceptan como verificados.

| Exit | Estado |
| --- | --- |
| 0 | completed: contrato explícito satisfecho |
| 2 | error |
| 3 | incomplete o blocked |
| 4 | unverified: respuesta útil, sin comprobación integral |

Sin contrato explícito el resultado no se anuncia como completed aunque use tools. Los scripts que antes tomaban exit 0 como «el modelo terminó» deben adaptar su tratamiento de exit 4.

```toml
[loop]
task_timeout = 300
max_tool_calls = 40
max_total_tokens = 24000

[skills]
enabled = ["troubleshooting"]
# root = "/ruta/a/skills-revisadas"  # alternativa a las empaquetadas
```

Skills disponibles: code-review, research, troubleshooting. Se carga solo lo elegido por CLI/configuración, con tope de 6.000 caracteres. No ejecutan scripts ni conceden permisos. No se instalan skills remotas automáticamente.

## MCP opcional

Cliente stdio limitado a versión 2025-06-18, initialize, tools/list paginado y tools/call textual. Sin HTTP remoto, OAuth, resources, prompts ni sampling. Las peticiones del servidor no soportadas se rechazan. No se presenta como implementación completa del protocolo.

Para no ejecutar servidores solo por listar herramientas, declara el catálogo revisado en configuración. El esquema debe coincidir con el servidor cuando se ejecuta. Usa `--tools` para inspeccionarlo sin arrancar procesos.

```toml
[[mcp.servers]]
name = "local"
command = ["python3", "/ruta/a/servidor_revisado.py"]
timeout = 10

[[mcp.servers.tools]]
name = "echo"
inputSchema = { type = "object", properties = { text = { type = "string" } }, required = ["text"] }
```

Nombre ofrecido: `mcp_local_echo`. Cada llamada pasa por aprobación; --read-only la deniega. --yes autoriza ask y debe usarse solo en una sesión de confianza. Autorizar la tool también autoriza ejecutar el comando configurado del servidor. Los clientes se abren bajo demanda y se cierran al terminar la tarea; se reutilizan dentro de ella. El entorno heredado está reducido, sin API keys implícitas.

El validador de argumentos cubre tipos, required, enum, properties, items y algunos límites; no implementa todo JSON Schema (por ejemplo, referencias y combinadores). Usa esquemas simples. Anotaciones del servidor no conceden permisos.

## Medición de lectura

Archivo local de 33.000.000 bytes, un millón de líneas; cinco repeticiones con tracemalloc, limit=1. Implementación actual: mediana **0,460 ms**, pico **21.825 bytes**. Devuelve primera línea numerada y marcador «hay más».

La auditoría anterior medía 837,51 ms / 82,5 MB, pero contaba exactamente las líneas restantes. El contrato cambió deliberadamente para dejar de recorrer todo el archivo. No es una comparación de salida idéntica ni un benchmark de inferencia. No se afirma que el agente completo sea esa proporción más rápido.

Prueba de modelo real desde wheel instalado fuera del checkout: init, doctor y escritura `notas.txt=hola`; exit 0 y contenido exacto comprobado. Una tarea simple no mide tasa de éxito general ni rendimiento de planificación.

## Límites y trabajo no completado

- Windows/macOS no se han ejecutado aquí; el workflow incorpora tests e instalación fuera del checkout para esas plataformas. La CI remota debe confirmar portabilidad.
- No se implementó streaming de tokens del modelo: no era una aceleración demostrada y requiere diseño de cancelación/UX separado.
- No hay benchmark A/B de inferencia, p50/p95 de tareas ni tasa de éxito de skills/MCP con modelos reales. Se mantiene la selección existente para no retirar capacidades sin evaluación.
- DNS se valida antes de conectar: existe un riesgo de rebinding/check-use; para un VPS expuesto se necesita filtrado de egreso o pinning. Chromium conserva flags sin sandbox del entorno previo; no procesa páginas hostiles con aislamiento garantizado.
- La stdlib no garantiza matar todos los descendientes desacoplados en Windows ni imponer límites de memoria/CPU del SO. Las escrituras MCP tienen timeout y detienen un servidor que no consume stdin; no uses servidores no confiables.
- La memoria y fuentes no tienen bloqueo entre procesos concurrentes. El diseño actual es secuencial por workspace; no ejecutar varias sesiones escritoras simultáneas.
- Grep limita tamaño por fichero y resultados, pero regex adversarias y árboles enormes requieren un motor/proceso aislado para un límite duro. read_file tiene máximo 64 MB y límite de exploración de un millón de líneas; una línea enorme dentro de ese máximo todavía se asigna temporalmente.
- El generador usa un workspace temporal propio. La captura de plan se rechaza si solo marca pasos; no se ha regenerado una captura aceptada de análisis semántico de cobertura. Las PNG antiguas son históricas.
- Doctor detecta extras, no garantiza navegador funcional ni tool-calling fiable del modelo.

## Comprobaciones locales

Comandos de referencia:

```bash
python3 -m unittest discover -s tests
python3 -m pyflakes harness agente.py tests docs/capturas/generar.py
python3 -m mypy harness agente.py --check-untyped-defs
python3 -m compileall -q harness agente.py tests docs/capturas/generar.py
bash -n instalar.sh
```

Mypy/pyflakes son herramientas de desarrollo, no dependencias del paquete. El wheel incluye las tres skills; no incluye las reglas personales. La configuración se genera con --init. No se ha publicado ni hecho push.
