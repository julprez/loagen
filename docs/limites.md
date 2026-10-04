# Límites

Lo que **no** se ha implementado a propósito:

* **Capa Multi-Agent** del diagrama (Subagent Spawner, Teammate Mailboxes, FSM
  Protocol, Autonomous Board, Worktree Isolator). Un 1.5B no sostiene coordinación
  multi-agente; es donde más se rompería. El bucle secuencial ya da resultado.
* **MCP Runtime / auto-discover**: cada servidor añade modos de fallo que el modelo
  no sabe depurar. Se puede añadir como herramienta más adelante.
* **Streaming y ejecución en paralelo**: con respuestas de ~50 tokens no aporta.
* **Aprobación no interactiva**: sin terminal, los permisos `ask` se deniegan. Es
  deliberado; para automatizar hace falta `--yes` explícito.
* El modelo puede **afirmar cosas sin haberlas hecho** cuando la respuesta no
  contiene un bloque de comandos. El harness mitiga los casos detectables y siempre
  separa "lo que dice el modelo" de "lo que el harness verificó"; no lo elimina.
  Medido: tras delegar, el padre afirmó "creé carpetas y descargué los ficheros" sin
  haber creado ni descargado nada (el registro de hechos del harness lo desmiente).
* **Se inventa detalles de lo que encuentra.** Medido: buscó "última versión de Python",
  encontró 3.14.8 en python.org (correcto) y se inventó la fecha de salida. Por eso el
  bloque verificado lista las **fuentes consultadas**: el harness no puede comprobar que
  cada frase salga de ahí, pero deja la lista a la vista para que la compruebes.
* **El subagente con `qwen2.5:1.5b-instruct` suele no compensar**: la maquinaria es
  correcta y segura (contexto y traza aislados, `read_only` por defecto, sin recursión),
  pero el modelo delega mal y el coste en tiempo es alto (una delegación midió 3m19s).
  Tiene sentido con un modelo mayor: `subagent_model = "qwen2.5-coder:3b"`.
* No hay paralelismo ni varios subagentes a la vez: es secuencial a propósito, para no
  abrir modos de fallo que un 1.5B no sabe depurar.
* **El guard del plan solo alcanza a los nombres de fichero.** Si la tarea no nombra
  ningún fichero no hay nada que comparar, y un plan que invente nombres pasa el filtro.
  Desde el arreglo del prompt es raro (ver [«Lo que medimos»](mediciones.md)), pero para
  una tarea importante conviene leer el plan que se imprime antes de dejarlo correr:
  el harness verifica el artefacto del plan, no el que pediste tú.
* **El plan no es exigible con este modelo.** Medido: la estructura del plan sale bien
  (3 pasos con dependencias correctas) y el trabajo se completa, pero el modelo a veces
  ignora los avisos y responde en prosa *«Plan marcado como done para el paso [1]»* sin
  llamar a `plan_update`. El harness no acepta esa frase como evidencia: el registro de
  hechos muestra `mkdir×1, write_file×1` y ningún `plan_update`, así que la afirmación
  queda desmentida a la vista. Los avisos están presupuestados (`plan_max_nudges`) para
  no gastar la ejecución peleándose con el modelo.
