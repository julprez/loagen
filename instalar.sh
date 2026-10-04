#!/usr/bin/env bash
# Instalador de loagen.
#
# No instala nada más: el harness solo usa la biblioteca estándar de Python. Lo que hace
# es comprobar los requisitos, descargar el modelo y escribir un `rules.toml` a medida.
#
#   ./instalar.sh                          # workspace = directorio actual
#   ./instalar.sh -w ~/mi-proyecto         # otro workspace
#   ./instalar.sh --model qwen2.5-coder:3b
#   ./instalar.sh --no-pull                # no descarga el modelo
#   ./instalar.sh --yes                    # no pregunta y sobrescribe rules.toml
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${LOAGEN_MODEL:-qwen2.5:1.5b-instruct}"
WORKSPACE="$(pwd)"
DO_PULL=1
ASSUME_YES=0

usage() {
  sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "$1"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -w|--workspace) WORKSPACE="${2:?falta la ruta}"; shift 2 ;;
    -m|--model)     MODEL="${2:?falta el modelo}"; shift 2 ;;
    --no-pull)      DO_PULL=0; shift ;;
    -y|--yes)       ASSUME_YES=1; shift ;;
    -h|--help)      usage 0 ;;
    *) echo "opción no reconocida: $1" >&2; usage 2 ;;
  esac
done

die()  { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

# --- 1. Python -------------------------------------------------------------
# Se busca 3.11+ porque `tomllib` (lo que lee `rules.toml`) entró en esa versión. Si solo
# hay 3.10, se avisa y se sigue: el harness arranca igual con los permisos por defecto.
TOML_OK=1
step "Buscando Python"
PY=""
for candidate in python3 python3.13 python3.12 python3.11; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c 'import sys, tomllib; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  # Sin `tomllib` el harness sigue funcionando (avisa y usa los permisos por defecto),
  # así que abortar aquí sería más estricto que el propio harness.
  for candidate in python3 python3.10; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$candidate"
      TOML_OK=0
      break
    fi
  done
fi
if [ -z "$PY" ]; then
  die "hace falta Python 3.10+.
       Debian/Ubuntu: apt install python3.11 (recomendado: 3.11+ para leer rules.toml)"
fi
echo "  $PY -> $("$PY" -V 2>&1)"
if [ "$TOML_OK" -eq 0 ]; then
  echo "  aviso: sin Python 3.11 no hay 'tomllib', así que rules.toml se ignorará" >&2
  echo "         y todo lo que modifica algo pedirá permiso. Instala 3.11+ para tener las reglas." >&2
fi

# --- 2. Ollama y el modelo -------------------------------------------------
step "Comprobando Ollama"
command -v ollama >/dev/null 2>&1 || die "no encuentro 'ollama' en el PATH.
       Instálalo desde https://ollama.com/download y vuelve a ejecutar esto."
if ! ollama list >/dev/null 2>&1; then
  die "ollama no responde. Arráncalo con: ollama serve"
fi
echo "  ollama -> $(command -v ollama)"

if ollama list | awk 'NR>1 {print $1}' | grep -qx "$MODEL" 2>/dev/null; then
  echo "  modelo '$MODEL' ya instalado"
elif [ "$DO_PULL" -eq 1 ]; then
  step "Descargando el modelo '$MODEL' (puede tardar)"
  ollama pull "$MODEL"
else
  echo "  aviso: '$MODEL' no está instalado y --no-pull está activo" >&2
  echo "         descárgalo luego con: ollama pull $MODEL" >&2
fi

# --- 3. Workspace y configuración -----------------------------------------
[ -d "$WORKSPACE" ] || die "el workspace no existe: $WORKSPACE"
WORKSPACE="$(cd "$WORKSPACE" && pwd)"
step "Configurando el workspace $WORKSPACE"

INIT_ARGS=(--init -w "$WORKSPACE" --model "$MODEL")
DO_INIT=1
if [ -f "$WORKSPACE/rules.toml" ]; then
  if [ "$ASSUME_YES" -eq 1 ]; then
    INIT_ARGS+=(--force)
  else
    echo "  ya existe $WORKSPACE/rules.toml"
    printf "  ¿sobrescribirlo? [s/N] "
    read -r answer || answer=""
    case "$answer" in
      s|S|y|Y|si|sí) INIT_ARGS+=(--force) ;;
      *) echo "  se conserva el rules.toml actual"; DO_INIT=0 ;;
    esac
  fi
fi
if [ "$TOML_OK" -eq 0 ]; then
  die "hace falta Python 3.11+; no se ignorarán las reglas de seguridad"
fi
if [ "$DO_INIT" -eq 1 ]; then
  "$PY" "$HERE/agente.py" "${INIT_ARGS[@]}"
fi

# --- 4. Diagnóstico --------------------------------------------------------
step "Diagnóstico"
"$PY" "$HERE/agente.py" --doctor -w "$WORKSPACE"

# --- 5. Cómo usarlo --------------------------------------------------------
cat <<EOF

Listo. Forma de usarlo sin instalar nada:

  "$PY" "$HERE/agente.py" -w "$WORKSPACE" "tu tarea"

EOF

if [ "$TOML_OK" -eq 1 ]; then
  cat <<EOF
Y como comando, en cualquier directorio (opcional):

  pipx install "$HERE"      # o: pip install --user "$HERE"
  cd "$WORKSPACE" && loagen "tu tarea"

EOF
else
  echo "(el paquete pip necesita Python 3.11+; con este Python usa la forma de arriba)"
  echo
fi

cat <<EOF
Para reconfigurar más adelante:  cd "$WORKSPACE" && loagen --init
EOF
