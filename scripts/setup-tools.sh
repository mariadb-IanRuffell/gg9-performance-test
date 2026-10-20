#!/usr/bin/env bash
# Downloads Apache JMeter and the GridGain 9 JDBC driver into ./tools (idempotent).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JMETER_VERSION="${JMETER_VERSION:-5.6.3}"
GG_VERSION="${GG_VERSION:-9.1.22}"

TOOLS="$ROOT/tools"
mkdir -p "$TOOLS"

if [ ! -x "$TOOLS/apache-jmeter-$JMETER_VERSION/bin/jmeter" ]; then
  echo ">> Downloading JMeter $JMETER_VERSION"
  curl -fSL "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-$JMETER_VERSION.tgz" -o "$TOOLS/jmeter.tgz"
  tar -xzf "$TOOLS/jmeter.tgz" -C "$TOOLS" && rm "$TOOLS/jmeter.tgz"
fi

LIB="$TOOLS/apache-jmeter-$JMETER_VERSION/lib"
if ! ls "$LIB"/ignite-client-*.jar >/dev/null 2>&1; then
  echo ">> Resolving GridGain JDBC driver $GG_VERSION + dependencies (maven)"
  rm -f "$LIB"/ignite-jdbc-*.jar
  mvn -q -f "$ROOT/framework/deps/pom.xml" -Dgg.version="$GG_VERSION" dependency:copy-dependencies \
      -DoutputDirectory="$LIB" -DincludeScope=runtime -DexcludeGroupIds=org.slf4j,ch.qos.logback
fi
echo "JMeter home: $TOOLS/apache-jmeter-$JMETER_VERSION"
