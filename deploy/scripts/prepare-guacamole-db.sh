#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

GUACAMOLE_VERSION="${GUACAMOLE_VERSION:-1.5.5}"
INIT_DIR="deploy/guacamole/initdb"
INIT_FILE="${INIT_DIR}/001-initdb.sql"

mkdir -p "$INIT_DIR"

if [[ -s "$INIT_FILE" ]]; then
  echo "Guacamole init SQL already exists: $INIT_FILE"
  exit 0
fi

echo "Generating Guacamole MySQL schema for version ${GUACAMOLE_VERSION}..."
docker run --rm "guacamole/guacamole:${GUACAMOLE_VERSION}" /opt/guacamole/bin/initdb.sh --mysql > "$INIT_FILE"

echo "Created $INIT_FILE"
echo "This file is used only on first MySQL volume initialization."
