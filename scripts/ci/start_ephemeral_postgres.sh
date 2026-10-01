#!/usr/bin/env bash
set -euo pipefail
# Throwaway PostgreSQL for native CI (.codebuild/ci.sh), so the gate's
# db_gated and uses_database suites run instead of skipping.
#
#   start_ephemeral_postgres.sh start   # logs on stderr, the DSN alone on stdout
#   start_ephemeral_postgres.sh stop    # idempotent; removes the data directory
#
# The server binds 127.0.0.1 only, uses trust auth (no password, no secret),
# keeps its data under /tmp, and runs with fsync off: it lives for one build.
# Source order: an installed PostgreSQL >= 16, then apt (the distro archive,
# then the PGDG archive that postgresql-common ships the key for), then
# docker when the build is privileged. Any failure exits non-zero with the
# reason and the server log, never a silent fallback to "no database".

PG_MIN_MAJOR=16
PG_PORT="${LEAF_CI_PG_PORT:-54329}"
PG_DB="${LEAF_CI_PG_DB:-leaf_gate}"
PG_USER=postgres
STATE_FILE="${LEAF_CI_PG_STATE:-/tmp/leaf-ci-postgres.state}"
DOCKER_NAME=leaf-ci-postgres

log() { printf 'ephemeral-postgres: %s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

as_root() {
  if [[ "$EUID" -eq 0 ]]; then "$@"; elif command -v sudo >/dev/null 2>&1; then sudo -n "$@"; else return 1; fi
}

as_pg() {
  if [[ "$EUID" -eq 0 ]]; then runuser -u "$PG_USER" -- "$@"; else "$@"; fi
}

# The newest installed server bin dir with major >= PG_MIN_MAJOR, or nothing.
find_pg_bin() {
  local dir major best="" best_major=0
  for dir in /usr/lib/postgresql/*/bin; do
    [[ -x "$dir/pg_ctl" && -x "$dir/initdb" ]] || continue
    major="${dir#/usr/lib/postgresql/}"
    major="${major%%/*}"
    [[ "$major" =~ ^[0-9]+$ ]] || continue
    if (( major >= PG_MIN_MAJOR && major > best_major )); then
      best="$dir"
      best_major="$major"
    fi
  done
  [[ -n "$best" ]] && printf '%s\n' "$best"
}

apt_install() {
  DEBIAN_FRONTEND=noninteractive timeout 600 apt-get install -y -q --no-install-recommends \
    -o Acquire::Retries=2 -o Acquire::http::Timeout=30 "$@" >&2
}

install_with_apt() {
  command -v apt-get >/dev/null 2>&1 || { log "apt-get unavailable"; return 1; }
  # The package must not create and start its own 'main' cluster on 5432.
  as_root mkdir -p /etc/postgresql-common || return 1
  if [[ ! -f /etc/postgresql-common/createcluster.conf ]]; then
    printf 'create_main_cluster = false\n' | as_root tee /etc/postgresql-common/createcluster.conf >/dev/null || return 1
  fi
  if as_root bash -c "$(declare -f apt_install); apt_install postgresql-$PG_MIN_MAJOR"; then
    return 0
  fi
  log "postgresql-$PG_MIN_MAJOR is not in the configured archives; adding PGDG"
  as_root timeout 300 apt-get update -q -o Acquire::Retries=2 -o Acquire::http::Timeout=30 >&2 \
    || log "apt-get update failed; trying the existing package lists"
  as_root bash -c "$(declare -f apt_install); apt_install postgresql-common ca-certificates" || return 1
  [[ -x /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh ]] || { log "PGDG helper missing"; return 1; }
  as_root timeout 300 /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y >&2 || return 1
  as_root bash -c "$(declare -f apt_install); apt_install postgresql-$PG_MIN_MAJOR"
}

start_native() {
  local bin="$1" data
  # initdb refuses root, so a root build runs the server as $PG_USER.
  if [[ "$EUID" -eq 0 ]] && ! id -u "$PG_USER" >/dev/null 2>&1; then
    as_root useradd --system --no-create-home --shell /usr/sbin/nologin "$PG_USER" >&2 || return 1
  fi
  data="$(mktemp -d /tmp/leaf-ci-postgres.XXXXXXXX)"
  chmod 700 "$data"
  [[ "$EUID" -ne 0 ]] || chown "$PG_USER" "$data"
  printf 'mode=native\nbin=%s\ndata=%s\n' "$bin" "$data" > "$STATE_FILE"
  log "initdb $("$bin/postgres" --version) in $data"
  as_pg "$bin/initdb" -D "$data" -U "$PG_USER" -A trust -E UTF8 --no-locale >&2 || return 1
  if ! as_pg "$bin/pg_ctl" -D "$data" -l "$data/server.log" -w -t 60 \
      -o "-c listen_addresses=127.0.0.1 -p $PG_PORT -k $data -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=200" \
      start >&2; then
    log "server log:"
    tail -n 50 "$data/server.log" >&2 || true
    return 1
  fi
  as_pg "$bin/createdb" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" "$PG_DB" >&2 || return 1
  as_pg "$bin/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -Atc \
    'select version()' >&2 || return 1
}

start_docker() {
  local i
  command -v docker >/dev/null 2>&1 && timeout 20 docker info >/dev/null 2>&1 || {
    log "docker unavailable (the build is not privileged)"
    return 1
  }
  printf 'mode=docker\nname=%s\n' "$DOCKER_NAME" > "$STATE_FILE"
  timeout 300 docker run -d --rm --name "$DOCKER_NAME" -p "127.0.0.1:$PG_PORT:5432" \
    -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB="$PG_DB" \
    "postgres:$PG_MIN_MAJOR" -c fsync=off -c synchronous_commit=off -c full_page_writes=off \
    -c max_connections=200 >&2 || return 1
  for i in $(seq 1 60); do
    if docker exec "$DOCKER_NAME" pg_isready -h 127.0.0.1 -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
      docker exec "$DOCKER_NAME" psql -h 127.0.0.1 -U "$PG_USER" -d "$PG_DB" -Atc 'select version()' >&2 || return 1
      return 0
    fi
    sleep 1
  done
  log "docker postgres never became ready; container log:"
  docker logs --tail 50 "$DOCKER_NAME" >&2 || true
  return 1
}

cmd_start() {
  local bin
  # Everything but the DSN goes to stderr, so the caller's $(...) captures
  # exactly one line no matter what apt, initdb or docker print.
  exec 3>&1 1>&2
  [[ "$PG_PORT" =~ ^[0-9]+$ ]] || die "LEAF_CI_PG_PORT must be numeric, got '$PG_PORT'"
  [[ "$PG_DB" =~ ^[a-z_][a-z0-9_]*$ ]] || die "LEAF_CI_PG_DB must be a plain identifier, got '$PG_DB'"
  [[ ! -e "$STATE_FILE" ]] || die "already started ($STATE_FILE exists); run stop first"
  bin="$(find_pg_bin || true)"
  if [[ -z "$bin" ]]; then
    log "no PostgreSQL >= $PG_MIN_MAJOR installed; installing"
    if install_with_apt; then
      bin="$(find_pg_bin || true)"
    else
      log "apt install failed"
    fi
  fi
  if [[ -n "$bin" ]]; then
    start_native "$bin" || die "native PostgreSQL from $bin did not start"
  else
    log "falling back to docker postgres:$PG_MIN_MAJOR"
    start_docker || die "no PostgreSQL >= $PG_MIN_MAJOR could be installed or started (apt and docker both failed)"
  fi
  log "ready on 127.0.0.1:$PG_PORT database $PG_DB"
  printf 'postgresql://%s@127.0.0.1:%s/%s\n' "$PG_USER" "$PG_PORT" "$PG_DB" >&3
}

cmd_stop() {
  local mode="" bin="" data="" name=""
  [[ -f "$STATE_FILE" ]] || { log "not running"; return 0; }
  mode="$(sed -n 's/^mode=//p' "$STATE_FILE")"
  bin="$(sed -n 's/^bin=//p' "$STATE_FILE")"
  data="$(sed -n 's/^data=//p' "$STATE_FILE")"
  name="$(sed -n 's/^name=//p' "$STATE_FILE")"
  case "$mode" in
    native)
      if [[ -x "$bin/pg_ctl" && -f "$data/postmaster.pid" ]]; then
        as_pg "$bin/pg_ctl" -D "$data" -m immediate -w -t 30 stop >&2 || log "pg_ctl stop failed"
      fi
      # Remove only a directory this script created.
      [[ "$data" == /tmp/leaf-ci-postgres.* ]] && rm -rf -- "$data"
      ;;
    docker)
      [[ -n "$name" ]] && docker rm -f "$name" >/dev/null 2>&1 || log "docker rm failed"
      ;;
    *)
      log "unknown state mode '$mode'"
      ;;
  esac
  rm -f -- "$STATE_FILE"
  log "stopped"
}

# The checkout is root-owned; initdb and pg_ctl run as the postgres user and
# must not inherit a working directory that user cannot read.
cd /tmp
case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
