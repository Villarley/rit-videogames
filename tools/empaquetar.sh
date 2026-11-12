#!/usr/bin/env bash
# Arma la entrega para el server del profe: una carpeta en .tar.gz con el repo de git adentro.
#   tools/empaquetar.sh Progra02            -> ../Progra02.tar.gz
#   tools/empaquetar.sh Progra03 docs/*.pdf -> además copia esos archivos dentro de la carpeta
# Usa "git clone" del repo local para que quede solo lo versionado (con .git),
# sin la colección, los índices ni los datos del buscador.
set -euo pipefail
nombre="${1:?uso: tools/empaquetar.sh PrograNN [archivos extra...]}"
shift
raiz="$(git rev-parse --show-toplevel)"
tmp="$(mktemp -d)"
git clone --quiet "$raiz" "$tmp/$nombre"
git -C "$tmp/$nombre" remote set-url origin "$(git -C "$raiz" remote get-url origin)"
for extra in "$@"; do cp "$extra" "$tmp/$nombre/"; done
tar -czf "$raiz/../$nombre.tar.gz" -C "$tmp" "$nombre"
echo "Listo: $(cd "$raiz/.." && pwd)/$nombre.tar.gz ($(du -h "$raiz/../$nombre.tar.gz" | cut -f1))"
echo "Contenido:"; tar -tzf "$raiz/../$nombre.tar.gz" | grep -v "/.git/" | head -40
