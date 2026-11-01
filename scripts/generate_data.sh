#!/usr/bin/env bash
# Generate the Riverside Clinic synthetic patients with Synthea.
# Pinned release, fixed seeds, fixed reference date and state, so anyone
# gets the same records. Output: CSV files in data/synthea/csv (git-ignored).
#
#   bash scripts/generate_data.sh          # skips if the same data is already there
#   FORCE=1 bash scripts/generate_data.sh  # regenerate from scratch
set -euo pipefail

SYNTHEA_VERSION="v4.0.0"
SYNTHEA_SHA256="ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1"
SYNTHEA_URL="https://github.com/synthetichealth/synthea/releases/download/${SYNTHEA_VERSION}/synthea-with-dependencies.jar"

SEED=20261101             # population seed and clinician seed
REFERENCE_DATE=20261031   # "today" inside the simulation; must match as_of in schema.json
POPULATION=10000          # living patients (only_alive_patients=true makes this exact)
STATE="Illinois"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JAR_DIR="$ROOT/.cache/synthea"
JAR="$JAR_DIR/synthea-with-dependencies-${SYNTHEA_VERSION}.jar"
OUT="$ROOT/data/synthea"
PARAMS="version=${SYNTHEA_VERSION} seed=${SEED} reference_date=${REFERENCE_DATE} population=${POPULATION} state=${STATE}"

# 1. Java 17 or newer
if ! command -v java >/dev/null 2>&1; then
  echo "error: java not found. Install Java 17+ (for example Temurin 17 or 21)." >&2
  exit 1
fi
JAVA_MAJOR="$(java -version 2>&1 | awk -F'"' '/version/ {print $2}' | cut -d. -f1)"
if [ -z "$JAVA_MAJOR" ] || [ "$JAVA_MAJOR" -lt 17 ]; then
  echo "error: Synthea needs Java 17+, found: $(java -version 2>&1 | head -n 1)" >&2
  exit 1
fi

# 2. Skip if this exact dataset already exists
if [ "${FORCE:-0}" != "1" ] && [ -f "$OUT/PARAMS" ] && [ "$(cat "$OUT/PARAMS")" = "$PARAMS" ]; then
  echo "Synthea data already generated ($PARAMS). Use FORCE=1 to regenerate."
  exit 0
fi

# 3. Download the pinned jar once and check its hash
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}
mkdir -p "$JAR_DIR"
if [ ! -f "$JAR" ]; then
  echo "Downloading Synthea ${SYNTHEA_VERSION} (about 200 MB)..."
  curl -fL --retry 3 -o "$JAR.part" "$SYNTHEA_URL"
  mv "$JAR.part" "$JAR"
fi
if [ "$(sha256_of "$JAR")" != "$SYNTHEA_SHA256" ]; then
  echo "error: $JAR does not match the pinned SHA-256. Delete it and run again." >&2
  exit 1
fi

# 4. Generate (10,000 patients takes several minutes and about 1.5 GB of disk)
rm -rf "$OUT"
mkdir -p "$OUT"
echo "Generating $POPULATION patients in $STATE (seed $SEED, as of $REFERENCE_DATE)..."
java -jar "$JAR" \
  -s "$SEED" -cs "$SEED" -r "$REFERENCE_DATE" -p "$POPULATION" \
  --exporter.baseDirectory="$OUT" \
  --exporter.csv.export=true \
  --exporter.csv.included_files=patients.csv,encounters.csv,conditions.csv,medications.csv,observations.csv \
  --exporter.fhir.export=false \
  --exporter.hospital.fhir.export=false \
  --exporter.practitioner.fhir.export=false \
  --exporter.metadata.export=false \
  --generate.only_alive_patients=true \
  "$STATE" > "$OUT/synthea.log" 2>&1 || {
    echo "error: Synthea failed. Last lines of $OUT/synthea.log:" >&2
    tail -n 20 "$OUT/synthea.log" >&2
    exit 1
  }

echo "$PARAMS" > "$OUT/PARAMS"
echo "Done. CSV files in data/synthea/csv:"
ls -lh "$OUT/csv"