# Lo que medimos

Datos reales de este equipo con `qwen2.5:1.5b-instruct` (no estimaciones):

* Emite **`tool_calls` nativos** por la API de Ollama: ~2,6–5,9 s por turno.
* `qwen2.5-coder:3b` **no** emite `tool_calls` nativos: devuelve el JSON como texto.
  Por eso el cliente también parsea texto (`llm.py`) y acepta ese modelo.
* Confundir carpeta con fichero: con `mkdir` separado y su descripción como guía
  negativa, la tarea «crea `demo/saludo.txt`» se resolvió **a la primera** (2 pasos,
  ~23 s). Sin ese arreglo, el modelo escribió un fichero llamado `demo`.
* El aviso de reintento **funciona como mensaje `user` y falla como `system`**
  (probado con 5 variantes). Reinyectar la prosa del modelo lo ancla en su propio
  error: hay que omitirla.
* El dialecto posicional (`write_file ruta "contenido"`) no interpreta escapes: sin
  convertir `\n` literales, Python da `SyntaxError`. Medido y corregido.
* Modelos pequeños escriben llamadas en 4 dialectos: nativo, JSON suelto,
  `herramienta(args)` y `herramienta arg1 arg2`. El cliente los normaliza todos.
* **El tope de tokens (`num_predict`) es imprescindible en CPU.** Sin él, un subagente se
  enrolló generando ~1.900 tokens a 11,6 tok/s y agotó los 180 s de timeout (log de
  Ollama: `cancel task, n_tokens = 2886`). Con tope, el peor turno baja a ~28-57 s y el
  guard de truncado lo reconduce en el turno siguiente (medido: 54 s → 4,9 s).
* **En CPU la latencia la manda el PROMPT, no la generación.** Del log de Ollama:
  `prompt eval = 12,3 ms/token` (≈81 tok/s) frente a ~11,6 tok/s de generación. Añadir el
  navegador y el cerebro subió el prompt de ~1018 a **~1464 tokens** (~18 s solo de
  evaluación), y la misma tarea pasó de 18 s a **31,8 s**. Comprimir el prompt de sistema
  y las descripciones de herramientas lo bajó a ~1235 tokens y la tarea a **24,0 s**
  (~25 % menos). Ollama sí reutiliza el prefijo (`cached n_tokens = 1203`), así que ese
  coste se paga una vez por sesión, no en cada paso.
* Un informe largo del subagente **desestabiliza al padre**: 1.467 caracteres de ruido
  bastaron para que entrara en un bucle de 512 tokens (57 s). De ahí el recorte a 1.000.
* El subagente **se inventa nombres de fichero** (`ls.py`, `ls2.py`) en vez de listarlos;
  hay que decirle explícitamente que los descubra con `list_dir`/`glob`.
* Un **plan de un solo paso** no aporta nada y dispara un aviso inútil: se descarta.
* Dos fallos de seguridad que aparecieron al escribir los tests: `--read-only` era
  anulable por una regla `allow` del fichero, y un subagente podía escribir en una sesión
  `--read-only`. Ambos corregidos y cubiertos por tests.
* El plan **sí ayuda pero no garantiza**: con el task graph activo, una tarea de 3 pasos
  se completó entera (ficheros escritos + test ejecutado, y el marcado en lote
  `plan_update('1,2,3,4')` funcionó: 4/4 en una sola llamada). En otra ejecución el mismo
  modelo cerró con pasos pendientes y hubo que recordárselo.
* **El planificador reescribía la tarea, y se arregló.** Medido al generar las capturas,
  con la misma tarea simple («escribe en notas.txt el texto hola»), el mismo modelo y 4
  ejecuciones por modo. «Limpia» = artefacto correcto en disco y sin avisos, reintentos ni
  otros alfabetos en la salida:

  | | salidas limpias | tiempo |
  | --- | --- | --- |
  | plan activado, **antes** | **0/4** | ~37 s |
  | plan activado, **después** | **4/4** | ~5 s |
  | plan desactivado | **4/4** | ~5 s |

  La causa estaba en el propio prompt: su ejemplo decía «crear la carpeta demo» y
  «escribir `demo/saludo.txt`», y el modelo **lo copiaba**. Para «escribe en `notas.txt`»
  devolvía «crear la carpeta demo» y «escribir `demo/notes.txt`» — otro fichero, en otra
  carpeta — y el harness lo aceptaba, porque verifica el artefacto **del plan**, no el que
  pediste. Dos arreglos: el prompt ya no enseña nombres inventados y prohíbe renombrar o
  añadir lo que no esté en la tarea; y un guard determinista (`contradicts_task`) descarta
  el plan si habla de otro fichero con la **misma extensión** que la tarea. Un nombre
  derivado (`calculadora` → `tests/test_calculadora.py`) se sigue aceptando.
* Aun así, `--init` genera **`plan = false`**: para una tarea de un paso el planificador
  suele devolver un plan de un paso, que el harness descarta (regla ya existente), así que
  no aporta nada y cuesta una llamada extra al modelo. Actívalo para tareas de varios
  pasos — ver [«Planificación (Task Graph)»](../README.md#planificación-task-graph).
* **`plan_require` empeora el resultado con 1.5b** (medido, por eso queda desactivado en
  `rules.toml`): al recibir el rechazo, el modelo se quedó repitiendo `mkdir` de un paso
  ya hecho — 9 pasos, 88,9 s y **cero ficheros** creados, frente a la ejecución sin
  exigirlo, que completó los 2 ficheros y el test. La maquinaria funciona (avisa, no se
  cuelga, cierra con AVISO); el modelo no sabe salir del rechazo. Actívalo con un modelo
  mayor.
* La tarea simple de 3 ficheros tarda **~50-90 s** y tiene mucha varianza (mismo prompt,
  misma máquina): en una tirada 56 s y completa, en otra degeneró. Es la varianza del
  modelo a temperatura 0, no del harness.
* **Las herramientas no son gratis: cada esquema se paga en el prompt.** Cada
  herramienta añade ~65-100 tokens de esquema, y en CPU eso es ~1 s de evaluación cada
  una. Con 19 herramientas el bloque de esquemas son ~5.024 caracteres (~1.256 tokens);
  con 14 son ~3.719 (~929).
* **Seleccionar herramientas por tarea baja la latencia de verdad** (misma tarea de
  código, mismo modelo, mismo equipo, dos ejecuciones seguidas):

  | | paso 1 | paso 2 |
  | --- | --- | --- |
  | catálogo completo (19 herramientas) | 27,92 s, **1702** tokens | 15,20 s, **1920** tokens |
  | selección por tarea (14, grupo `code`) | 20,83 s, **1422** tokens | 12,15 s, **1640** tokens |

  Son **280 tokens** menos y **7,1 s** menos en el primer paso (y 280/3,0 s en el
  segundo), con la misma herramienta elegida (`cerebro_buscar`) en ambas. Una tarea
  ambigua habría recibido las 19. Nota: Ollama reutiliza el prefijo, así que el ahorro se
  concentra en el arranque y cuando el prefijo cambia, no en cada turno.
* **`cerebro_defs`/`cerebro_callers` encuentran lo que `impact` no distingue.** Con
  `run_search` real: `defs` da las 2 definiciones (prototipo y real) y `callers` los 11
  puntos de uso directo con su número de referencias; `impact` da el árbol transitivo
  pero no dice cuántas veces se usa cada uno.
* **El registro de fuentes tenía dos fallos que solo aparecieron ejecutándolo de verdad**
  (no en los tests): (1) una página que primero salía en resultados y luego se leía con
  `fetch_url` se quedaba marcada `[NO leída]`, así que el guard de citación no la veía
  — ahora la entrada **se asciende** a leída conservando su número; (2) el guard no se
  apagaba al citar `[1]`, porque solo buscaba la URL en el texto: el modelo citaba
  correctamente y aun así gastaba los reintentos en un aviso ya cumplido. Los dos están
  cubiertos por tests, incluido uno de punta a punta del bucle.
