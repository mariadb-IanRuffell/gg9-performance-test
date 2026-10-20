#!/usr/bin/env bash
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
docker compose -f "$ROOT/cluster/local/docker-compose.yml" down -v
