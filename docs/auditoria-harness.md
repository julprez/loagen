# Auditoría del harness loagen

> **Documento histórico.** Audita el estado del harness *antes* de la tanda de
> correcciones del mismo día, recogida en [`mejoras-harness.md`](mejoras-harness.md).
> Los hallazgos P0 y P1 se corrigieron después; consérvalo como registro de lo que se
> encontró, no como descripción del código actual. En particular, hoy `pyflakes` y
> `mypy --check-untyped-defs` pasan limpios y la suite tiene **223 pruebas** (no 197).

Fecha: 4 de octubre de 2026. Alcance: código actual de `agente.py`, núcleo `harness/`, tests, instalación, empaquetado, CI y generación de capturas. No se han cambiado permisos, herramientas ni comportamiento del producto.

## Dictamen

La base es adecuada para un agente local ligero: stdlib, registro explícito, selección determinista de herramientas, límites de generación y pasos, memoria y trazas. No conviene sustituirla por un framework pesado.

Antes de ampliar funcionalidades hay que corregir el PermissionGate y distinguir ejecución intentada, ejecución exitosa y tarea verificada. Actualmente hay promesas de seguridad y verificación que el código no cumple. Organizar carpetas ayuda al mantenimiento, pero por sí solo no reduce latencia ni corrige estos problemas.

**Estado real:** hay herramientas nativas y perfiles de selección; no hay un cargador de skills ni un cliente/servidor MCP implementado. El navegador y cerebro son adaptadores Python/subprocess, no conexiones MCP.

## Evidencia y límites

- Linux, Python 3.12: `python3 -m unittest discover -s tests`: **197 tests OK** en la fecha de esta auditoría (hoy son 223), dos ejecuciones finales, 0,118–0,127 s.
- `compileall` de harness, CLI, tests y generador: OK. `bash -n instalar.sh`: OK.
- Pyflakes completo: **fallaba** por import no utilizado en `harness/navegador.py:226` (corregido; hoy pasa limpio, igual que `mypy --check-untyped-defs`).
- Reproducciones de permisos, filesystem y bucle con directorios temporales y modelos falsos deterministas. No se leyeron secretos reales ni se ejecutaron comandos destructivos.
- La falsa finalización se reprodujo a través de `agente.run_task`, la misma interfaz de tarea que usa el CLI. La reparación de configuración se probó invocando `agente.py --init --force` como proceso real.
- No se han ejecutado Windows/macOS, CI remota ni una evaluación nueva de modelos reales. Los resultados deterministas prueban defectos del harness, no tasas de fallo de Ollama.
- Búsqueda inicial en cerebro: sin resultados para loagen. No se reconstruyó el índice ni se modificaron otros proyectos.

## Hallazgos prioritarios

### P0 — Seguridad y permisos

**1. `--read-only` puede ser anulado por una regla allow.**

`Config.action_for` aplica el modo de lectura antes de las reglas, pero `PermissionGate.check` busca y aplica las reglas por su cuenta antes de llamar a esa función. Con `read_only=True` y una regla allow de escritura, `action_for` devuelve deny y el gate devuelve permitido. Una ejecución completa con modelo falso escribió `proof.txt`.

Remedio: una única decisión de política, con restricciones absolutas antes de reglas y aprobaciones; tests de gate y del bucle, no solo de Config. La política debe propagarse sin relajarse a subagentes y futuros proveedores MCP.

**2. El confinamiento de rutas es solo léxico.**

`resolve_path` usa abspath, no resuelve enlaces simbólicos. Un enlace dentro del workspace a una carpeta exterior permite leer y escribir allí con `allow_outside=False`. `_h_glob` ni siquiera pasa por resolve_path: `../outside/*` devuelve ficheros externos. Grep puede abrir enlaces a ficheros externos.

Remedio: política canónica común para todas las operaciones, incluidos patrones y resultados de búsquedas; validar destinos reales y padres existentes de nuevas rutas. Si se exige aislamiento frente a modificaciones concurrentes del filesystem, hacen falta operaciones seguras frente a carreras o aislamiento del sistema operativo: realpath por sí solo no es un sandbox.

**3. Un prefijo de lectura autoriza comandos compuestos que escriben.**

Las reglas generadas permiten `ls; printf fixture > audit.txt` sin preguntar. Un match regex al comienzo del comando no describe sus efectos. Bash tampoco está confinado por el cwd.

Remedio ligero: no autoautorizar shell arbitrario por prefijo; ofrecer operaciones tipadas para lectura/git, y pedir aprobación para shell libre. No intentar hacer un sandbox con una lista negra de regex.

**4. La aprobación puntual del usuario no funciona.**

Cuando `_prompt` devuelve `allow`, `check` deja `action='ask'` y termina denegando. La respuesta `always` sí cambia action. Reproducido simulando una terminal y un usuario que aprueba una vez.

Remedio: separar decisión de política y respuesta de aprobación; mantener las aprobaciones de sesión en una instancia, no en el conjunto global `_ALWAYS` compartido por todo el proceso.

### P1 — Fiabilidad y resultados

**5. «Verificado» puede incluir una operación fallida.**

`Trace.facts` obtiene ficheros y fuentes de `tool_call`, emitido antes de ejecutar. Una escritura con `tool_error` sigue apareciendo como fichero en facts. `used_tools` también cuenta intentos, por lo que el informe del subagente no garantiza resultados exitosos.

Remedio: identificador por llamada, resultado estructurado y resumen basado en operaciones exitosas. Etiquetar por separado intentado / exitoso / comprobado. Leer una URL no verifica que todas las afirmaciones finales se desprendan de ella.

**6. Finalizar el modelo no significa completar la tarea.**

Una respuesta falsa «He creado missing.txt» sin herramientas es aceptada como final y `run_task` devuelve exit 0 aunque el fichero no exista. Marcar un plan done no requiere evidencia. Agotar pasos o rechazos tampoco produce siempre fallo en el CLI.

Remedio: estados `completed`, `incomplete`, `blocked`, `error` y `unverified`, con comprobaciones de aceptación cuando la tarea las define. Para consultas abiertas no prometer verificación semántica automática. Los requisitos de salida no deben extraerse solo del plan inventado por el modelo.

**7. La compresión no garantiza caber en contexto.**

No cuenta esquemas ni argumentos de tool_calls; no recorta si quedan pocos mensajes; el digest crece y puede partir una pareja llamada/observación. Con num_ctx=1024 el presupuesto calculado era 3.481 caracteres, pero devolvió mensajes serializados de 50.213 caracteres con un argumento de escritura grande.

Remedio: presupuestar todo el payload más reserva de salida, proteger tarea original y parejas de llamadas/resultados, mantener resumen acotado y evitar reinyectar contenidos grandes de escrituras cuando un recibo basta. Usar contadores reales del proveedor para ajustar la estimación, no convertir caracteres en una garantía de tokens.

**8. La memoria favorece notas antiguas y oculta las recientes.**

Se añade al final y se lee el principio. Una nota nueva tras superar max_chars no llega al modelo. Reproducido con marcador nuevo ausente en Memory.read.

Remedio: límites persistentes, selección de notas recientes/relevantes, procedencia y separación entre datos no confiables e instrucciones. Los subagentes comparten almacenamiento aunque sus historiales sean aislados.

**9. Instalación y diagnóstico tienen caminos incompletos.**

- Configuración TOML malformada bloquea `--init --force` con traceback porque se carga antes de entrar al inicializador. Reproducido con proceso CLI real.
- Si se rechaza sobrescribir reglas en instalar.sh, igualmente se invoca init sin force; devuelve 2 y set -e aborta. Hallazgo por lectura, no prueba completa del instalador.
- El paquete declara harness y agente, pero no distribuye rules.toml como datos: el fallback del checkout no debe presentarse como configuración instalada garantizada.
- Se permite continuar sin tomllib y sin reglas deny; con --yes puede debilitar la política. El paquete ya exige Python >=3.11: mejor error claro que degradación insegura.
- Windows/Git Bash usando executable con shell=True no está validado; cerebro ejecuta `python3` fijo en lugar del intérprete activo.
- Doctor comprueba presencia de Playwright, no disponibilidad real de Chromium ni capacidades del modelo.

**10. La demo de plan no demuestra completar su tarea.**

`plan_task_is_good` solo busca plan, plan_update y texto «verificado»; no exige leer los tres archivos ni evaluar cobertura. El generador borra notas.txt y fuerza reglas en el workspace usado: debería crear un workspace temporal propio y fixtures reproducibles.

### Riesgos adicionales a cubrir

Fetch de cualquier HTTP(S), redirecciones y Chromium sin sandbox merecen una política explícita de red, especialmente en VPS: loopback, red privada y metadatos pueden ser recursos sensibles, aunque sean útiles en un agente local. El texto de páginas, memoria, skills y descripciones MCP es entrada no confiable, no autoridad para conceder permisos. Las trazas guardan argumentos y fragmentos de resultados sin redacción; tienen colisiones posibles por IDs con precisión de segundos y silencian fallos de escritura. La conversación reutiliza Trace entre tareas, mezclando used_tools/facts de encargos anteriores.

## Eficiencia: medida y propuesta

### Lectura de ficheros

Fixture: 1.000.000 líneas, 33 MB; `read_file(path, limit=1)`. Cinco repeticiones por variante con tracemalloc; mediana local, no benchmark de producción.

| Variante | Mediana | Pico de asignaciones Python |
| --- | ---: | ---: |
| Handler actual, readlines completo | 837,51 ms | 82.462.516 bytes |
| Prototipo streaming, salida idéntica incluido número de líneas restantes | 424,93 ms | 22.151 bytes |

El prototipo solo cubre offset=1/limit=1 y no se integró. Conserva el recuento exacto y por ello aún recorre el archivo completo. Omitir el recuento exacto y devolver «hay más líneas» permitiría parar pronto, pero cambia el contrato y necesita tests. No extrapolar esta medición al tiempo total del agente.

### Prioridad de optimizaciones

1. **Limitar trabajo y memoria antes de recortar la salida:** lectura incremental, topes de líneas/bytes, glob incremental, captura acotada de procesos. Actualmente capture_output y glob acumulan resultados antes de recortar; max_output no limita recursos consumidos.
2. **Presupuesto global:** tiempo total, tokens y llamadas, heredado por subagentes; límites de procesos y de salida. max_steps cuenta turnos, no llamadas: un turno puede ejecutar varias herramientas y abrir varios subagentes.
3. **Catálogo pequeño y adaptable:** conservar perfiles, filtrar por permisos/capacidades y permitir activación explícita de herramientas que falten. La selección está congelada y puede eliminar una capacidad necesaria; todos los perfiles mantienen ocho herramientas filesystem/bash aunque no las necesiten.
4. **Skills bajo demanda:** cargar índice corto y solo el cuerpo de la skill elegida. No añadir todo el catálogo al prompt ni pagar una llamada de modelo solo para clasificar.
5. **Menos llamadas auxiliares:** plan/subagentes opcionales, encargos concretos y verificables; evitar delegación por defecto en CPU. No paralelizar inferencia hasta medir contención del único servidor/modelo local.
6. **Medir el proveedor:** conservar load_duration, prompt_eval_duration, eval_duration, motivo de fin y tiempos de herramientas/plan/cierre. Hoy solo se retienen algunos contadores y plan/cierre no forman parte del total de model_call del resumen.
7. **Memoria/fuentes:** caché por sesión y refresco tras subagentes; deduplicación y escrituras atómicas. SourceLedger relee todo el JSONL por cada add/find/uncited. Primero medir: el coste suele ser menor que inferencia.
8. **Streaming opcional:** mejora percepción de respuesta y cancelación; no demuestra por sí solo menos tiempo total de cómputo.

La documentación cita ahorros anteriores por selección de tools, pero no se ha repetido una comparativa Ollama aquí. No se afirma un nuevo ahorro de inferencia.

## Organización propuesta

Separar responsabilidades sin cambiar nombres públicos de herramientas ni convertir todo a MCP:

```text
harness/
  core/          # bucle, sesión, presupuesto, estado de ejecución, contexto
  providers/     # adaptador Ollama, capacidades, dialecto de tool calls
  tools/         # contrato/registro + filesystem, shell, web, cerebro, coordinación
  policy/        # permisos, rutas, red, aprobaciones
  skills/        # índice y carga de instrucciones bajo demanda
  mcp/           # cliente/adaptador opcional; no un nuevo bucle del agente
  storage/       # memoria, fuentes, trazas
skills/
  code-review/SKILL.md
  research/SKILL.md
  troubleshooting/SKILL.md
```

Este es un destino orientativo, no una migración obligatoria de todos los ficheros. Empezar por fronteras reales y mantener compatibilidad de imports/CLI en cada paso. Evitar un sistema de plugins general hasta tener usos concretos.

### Tools

Funciones ejecutables y verificables. Un contrato común debe incluir nombre, esquema, proveedor, efectos, timeout y límites; devolver `ToolResult` con ok, contenido, truncado, artefactos y evidencia. Validar tipos/rangos/argumentos antes de ejecutar. Mutating actualmente es una etiqueta de presentación, no la autoridad del PermissionGate.

La puerta de permisos será única para herramientas nativas y externas. No eliminar bash: mantenerlo como escape hatch autorizado, no como operación de lectura confiable por regex.

### Skills

Instrucciones reutilizables, no procesos ni nuevas autoridades. Metadatos mínimos: nombre, descripción, cuándo usarla y herramientas necesarias. El cuerpo contiene procedimiento y criterios de comprobación. Puede declarar herramientas requeridas para solicitar activación, pero no concede permisos.

Primera versión: skills locales revisadas, activación explícita y selección determinista opcional. No ejecutar scripts incluidos ni instalar contenido de Internet automáticamente. Mantener índice/body fuera del prefijo cuando no se usan, con presupuesto de contexto.

### MCP

Loagen actuaría como **host con clientes MCP**, consumiendo herramientas externas. MCP también distingue resources y prompts: no son sinónimos de tools ni de skills locales.

Primera versión recomendada: opcional, un servidor local explícito, stdio, arranque bajo demanda, negociación de versión/capacidades, tools/list y tools/call, límites/cancelación y cierre del proceso. Nombres con namespace y catálogo filtrado antes de enviarlo al modelo. El comando de arranque necesita autorización: abrir un servidor ya ejecuta código aunque todavía no se llame una tool.

No confiar automáticamente en anotaciones readOnly/destructive ni en descripciones del servidor. Cualquier herramienta externa sin política revisada debe pedir autorización; --read-only no puede asumir que es segura. Activar solo servidores configurados por el usuario y minimizar el entorno heredado.

Para HTTP remoto, revisar autorización, destinos, secretos y consentimiento por separado. No implementar OAuth parcial ni presentar una versión stdio mínima como soporte MCP completo. Elegir dependencia/SDK o stdlib mediante decisión explícita: un SDK opcional aporta conformidad, pero cambia el objetivo de cero dependencias; implementar todo el protocolo a mano no es necesariamente más simple.

Fuentes oficiales consultadas para esta propuesta (la versión final deberá fijar y probar su compatibilidad):
- [Especificación MCP 2025-06-18: arquitectura, primitivas y consentimiento](https://modelcontextprotocol.io/specification/2025-06-18).
- [Buenas prácticas de seguridad MCP, borrador](https://modelcontextprotocol.io/docs/draft/tutorials/security/security_best_practices).

## Orden de implementación y aceptación

1. **Permisos:** read-only absoluto, aprobación puntual, confinamiento filesystem y retirada de allow por prefijo shell. Tests de integración negativos y positivos, incluidos subagentes. Sin pretender sandbox de procesos.
2. **Resultados:** ToolResult y correlación llamada/resultado, estados de tarea y exit codes documentados; comprobar artefactos donde exista contrato. Una tool fallida no aparece como operación exitosa.
3. **Recursos/contexto:** streaming filesystem/procesos, presupuesto global y compresión íntegra. Benchmarks pequeños/grandes con salida correcta, tiempo y memoria antes/después.
4. **Distribución:** init reparable, instalación conservadora, wheel probado fuera del checkout y CI real Linux/Windows/macOS. Corregir docs/capturas según evidencia.
5. **Skills:** tres skills locales bajo demanda y activación comprobable; ninguna puede elevar permisos. Medir payload y éxito frente a baseline.
6. **MCP:** adaptador opcional con un servidor fixture, errores, timeout, cierre, nombres, esquema y política; sin depender de un proveedor remoto para tests.

Evaluación futura: tareas de escritura exacta, lectura/cobertura, reparación de bug con tests, investigación con fuentes y denegaciones. Registrar tasa de artefactos correctos, falsos éxitos, llamadas, tokens, p50/p95 y memoria; controlar modelo, caché, configuración y orden de ejecuciones. La métrica principal debe ser **coste por tarea correctamente terminada**, no tokens por segundo ni planes marcados done.
