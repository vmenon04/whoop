#!/usr/bin/env sh
# Regenerate pydantic models from the WHOOP OpenAPI spec. Never edit generated.py by hand.
set -eu
cd "$(dirname "$0")/.."
uv run datamodel-codegen \
  --input .claude/skills/whoop-api/reference/openapi.json \
  --input-file-type openapi \
  --output src/whoop_lab/whoop/generated.py \
  --output-model-type pydantic_v2.BaseModel \
  --target-python-version 3.12 \
  --use-standard-collections \
  --use-union-operator \
  --use-schema-description \
  --disable-timestamp
