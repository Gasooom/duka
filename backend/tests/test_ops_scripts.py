"""Operator scripts never put a secret on a command line, where every local user can read it in the process list, nor
in their output. Values reach containers by name (`-e VAR`) or from an env file, and reach curl on stdin."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from secrets import token_hex
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|API_KEY|DATABASE_URL", re.I)
# `-e NAME=…`, `--env NAME=…`, `--env=NAME=…`, the pair optionally quoted; never `--env-file …` or `-e NAME` alone.
ENV_WITH_VALUE = re.compile(r"(?:^|\s)(?:-e\s+|--env(?:\s+|=))[\"']?([A-Za-z_][A-Za-z0-9_]*)=")
# `-H "Authorization: …"` written into a command; `-H @-` (the header read from stdin) is how it is done.
INLINE_AUTH = re.compile(r"(?:^|\s)(?:-H|--header)\s*[\"']?Authorization:", re.I)


def secret_values(text: str) -> list[str]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        hits += [f"{n}: {name}" for name in ENV_WITH_VALUE.findall(line) if SECRET_NAME.search(name)]
        if INLINE_AUTH.search(line):
            hits.append(f"{n}: Authorization header")
    return hits


@pytest.mark.parametrize("line", [
    '-e DATABASE_URL="postgresql+psycopg://u:$PW@db:5432/x"',
    '-e VERIFY_PASSWORD="$VERIFY_PASSWORD" backend',
    "--env POSTGRES_PASSWORD=$DB_PASSWORD",
    "docker run --env=OPENAI_API_KEY=$KEY image",
    '-e "JWT_SECRET=$JWT"',
    "-e 'OPS_TOKEN=abc'",
    'curl -fs $K -H "Authorization: Bearer $OPS" $BASE/metrics',
    "curl --header 'authorization: Bearer abc' $API",
])
def test_a_secret_value_on_a_command_line_is_caught(line):
    assert secret_values(line) != []


@pytest.mark.parametrize("line", [
    "-e DATABASE_URL -e VERIFY_EMAIL -e VERIFY_PASSWORD backend",  # by name: the value comes from the environment
    "-e BACKGROUND_WORKERS=0",  # not a secret
    '-e VERIFY_DATABASE="$SCRATCH"',  # a database name, not its URL
    'COMPOSE="docker compose --env-file deploy/.env.production -f deploy/docker-compose.prod.yml"',
    "docker compose --env-file=deploy/.env.production run backend",
    'export DATABASE_URL="postgresql+psycopg://u:$PW@db:5432/x"',  # a shell builtin, not a process argument
    "printf 'Authorization: Bearer %s\\n' \"$OPS\" | curl -s $K -H @- \"$@\"",  # printf is a builtin; curl reads stdin
])
def test_names_env_files_and_plain_values_are_allowed(line):
    assert secret_values(line) == []


def test_no_operator_script_passes_a_secret_as_a_command_line_value():
    if not SCRIPTS.is_dir():
        pytest.skip("scripts/ is not part of this checkout (the backend image holds backend/ only)")
    scripts = sorted([*SCRIPTS.glob("*.sh"), *(ROOT / "deploy").glob("*.sh")])
    assert {"verify_restore.sh", "demo_chat.sh", "verify_deployment.sh"} <= {p.name for p in scripts}
    found = {p.name: secret_values(p.read_text(encoding="utf-8")) for p in scripts}
    assert {name: hits for name, hits in found.items() if hits} == {}


# The scripts run for real, with every external command they use replaced by a stub that records its arguments: curl
# and docker are fakes that record their stdin too (no network, no containers); the others run the real program.
BASH = shutil.which("bash")
PASSTHROUGH = ["grep", "cut", "sed", "sort", "openssl"]
LOG_ARGS = '#!/usr/bin/env bash\nprintf "%s\\n" "${0##*/} $*" >> "$STUB_LOG/argv"\n'
RECORD_STDIN = r"""cat > "$STUB_LOG/last"; { printf '%s <<' "${0##*/}"; cat "$STUB_LOG/last"; printf '>>\n'; } >> "$STUB_LOG/stdin"
"""
# The API: answers login and the simulator, and accepts a webhook only when it is signed with ITS secret.
FAKE_CURL = RECORD_STDIN + r"""case "$*" in
  *auth/login*) printf '{"access_token":"%s"}' "$FAKE_TOKEN" ;;
  *dev/simulate*) printf '{"status":"ok","reply":"hi"}' ;;
  *X-Hub-Signature-256:*)
    prev=; for a; do [ "$prev" = -d ] && body=$a; case $a in X-Hub-Signature-256:*) sig=${a#*sha256=} ;; esac; prev=$a; done
    want=$(printf '%s' "$body" | KEY=$FAKE_API_SECRET "$STUB_PYTHON" -c 'import hashlib, hmac, os, sys
print(hmac.new(os.environ["KEY"].encode(), sys.stdin.buffer.read(), hashlib.sha256).hexdigest())')
    [ "${sig%$'\r'}" = "${want%$'\r'}" ] && printf 200 || printf 401 ;;
esac
"""
# `exec … python -c CODE` runs CODE here, as in the API container, whose own copy of the App secret is FAKE_API_SECRET.
FAKE_DOCKER = RECORD_STDIN + r"""case "$*" in *"-e DATABASE_URL "*) printf '%s\n' "$DATABASE_URL" >> "$STUB_LOG/database_url" ;; esac
args=("$@")
for i in "${!args[@]}"; do
  if [ "${args[i]}" = python ] && [ "${args[i+1]}" = -c ]; then
    WHATSAPP_APP_SECRET=$FAKE_API_SECRET exec "$STUB_PYTHON" -c "${args[i+2]}" < "$STUB_LOG/last"
  fi
done
"""
INHERITED = re.compile(r"^(POSTGRES_\w+|VERIFY_\w+|WHATSAPP_\w+|FAKE_\w+|COMPOSE|PASSWORD|API|DATABASE_URL|DUKA_ENV_FILE"
                       r"|BACKUP_DIR|CURL_INSECURE)$")


def run_stubbed(tmp_path: Path, script: Path, *args: str, env: dict[str, str]) -> SimpleNamespace:
    if not SCRIPTS.is_dir() or BASH is None:
        pytest.skip("needs the repository's scripts and a bash")
    found = subprocess.run([BASH, "-c", 'for n; do command -v "$n" || echo; done', "_", *PASSTHROUGH],
                           capture_output=True, text=True).stdout.splitlines()
    real = dict(zip(PASSTHROUGH, found))
    if not all(real.get(n) for n in ("grep", "cut")):
        pytest.skip("bash cannot find grep/cut")
    bin_dir, log = tmp_path / "bin", tmp_path / "log"
    bin_dir.mkdir()
    log.mkdir()
    stubs = {"curl": FAKE_CURL, "docker": FAKE_DOCKER, "python3": 'exec "$STUB_PYTHON" "$@"\n',
             **{n: f'exec "{real[n]}" "$@"\n' if real.get(n) else "exit 127\n" for n in PASSTHROUGH}}
    for name, body in stubs.items():
        (bin_dir / name).write_bytes((LOG_ARGS + body).encode())  # LF endings on every OS
        (bin_dir / name).chmod(0o755)
    clean = {k: v for k, v in os.environ.items() if not INHERITED.match(k)}
    proc = subprocess.run([BASH, script.as_posix(), *args], stdin=subprocess.DEVNULL, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          env={**clean, **env, "STUB_LOG": log.as_posix(), "PYTHONIOENCODING": "utf-8",
                               "STUB_PYTHON": Path(sys.executable).as_posix(),
                               "PATH": os.pathsep.join([str(bin_dir), os.environ["PATH"]])})

    def read(name: str) -> str:
        return (log / name).read_text(encoding="utf-8") if (log / name).exists() else ""

    return SimpleNamespace(returncode=proc.returncode, output=proc.stdout + proc.stderr, argv=read("argv"),
                           stdin=read("stdin"), database_url=read("database_url"))


def test_demo_chat_sends_the_password_and_the_session_token_on_stdin(tmp_path):
    password, token = f"dummy-pw-{token_hex(8)}", f"dummy-token-{token_hex(8)}"
    run = run_stubbed(tmp_path, SCRIPTS / "demo_chat.sh", "owner@shop.test", "250700000000", "hello",
                      env={"PASSWORD": password, "FAKE_TOKEN": token})
    assert run.returncode == 0, run.output
    for secret in (password, token):
        assert secret not in run.argv and secret not in run.output
    assert f'"password":"{password}"' in run.stdin and f"Authorization: Bearer {token}" in run.stdin


@pytest.mark.parametrize("api_has_the_configured_secret", [True, False])
def test_verify_deployment_signs_with_the_configured_secret_and_keeps_secrets_off_command_lines(
        tmp_path, api_has_the_configured_secret):
    ops, verify, app = (f"dummy-{kind}-{token_hex(8)}" for kind in ("ops", "verify", "app"))
    api_secret = app if api_has_the_configured_secret else f"dummy-api-{token_hex(8)}"
    env_file = tmp_path / "env.production"
    env_file.write_bytes(f"DUKA_DOMAIN=duka.invalid\nOPS_TOKEN={ops}\nWHATSAPP_VERIFY_TOKEN={verify}\n"
                         f"WHATSAPP_APP_SECRET={app}\n".encode())
    run = run_stubbed(tmp_path, ROOT / "deploy" / "verify_deployment.sh",
                      env={"DUKA_ENV_FILE": env_file.as_posix(), "FAKE_API_SECRET": api_secret})
    assert re.search(r"\d+ passed, \d+ failed", run.output), run.output  # every check ran (the fakes fail most)
    # a stale or different secret in the running API fails the check
    assert f"{'PASS' if api_has_the_configured_secret else 'FAIL'}  signed webhook accepted" in run.output
    for secret in (ops, verify, app, api_secret):
        assert secret not in run.argv and secret not in run.output
    assert run.stdin.count(f"Authorization: Bearer {ops}") == 2  # metrics, readiness details
    assert f"hub.verify_token={verify}&" in run.stdin
    assert f"docker <<{app}\n" in run.stdin  # the configured secret reaches the signer on stdin


def restore_tree(tmp_path: Path, dotenv: str | None = None) -> tuple[Path, str]:
    """verify_restore.sh in an otherwise empty checkout (restore.sh succeeds at once), with a backup file to verify."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "verify_restore.sh", repo / "scripts")
    (repo / "scripts" / "restore.sh").write_bytes(b"#!/usr/bin/env bash\nexit 0\n")
    (repo / "scripts" / "restore.sh").chmod(0o755)
    if dotenv is not None:
        (repo / ".env").write_bytes(dotenv.encode())
    (repo / "duka-test.dump").write_bytes(b"")
    return repo / "scripts" / "verify_restore.sh", (repo / "duka-test.dump").as_posix()


@pytest.mark.parametrize("given, missing", [
    ({"POSTGRES_USER": "duka"}, "POSTGRES_PASSWORD"),
    ({"POSTGRES_PASSWORD": "dummy-db-pw"}, "POSTGRES_USER"),
])
def test_verify_restore_on_another_stack_stops_before_restoring_without_credentials(tmp_path, given, missing):
    script, dump = restore_tree(tmp_path)
    run = run_stubbed(tmp_path, script, dump, env={"COMPOSE": "docker compose -f prod.yml", **given})
    assert run.returncode == 1
    assert f"{missing} is not set" in run.output and "dummy-db-pw" not in run.output
    assert "docker" not in run.argv  # nothing restored, nothing to clean up


def test_verify_restore_keeps_the_development_stack_defaults(tmp_path):
    script, dump = restore_tree(tmp_path)
    run = run_stubbed(tmp_path, script, dump, env={})
    assert run.returncode == 0, run.output
    assert run.database_url.startswith("postgresql+psycopg://commerce:commerce@db:5432/duka_verify_")


def test_verify_restore_reads_a_password_containing_equals_signs_from_dot_env(tmp_path):
    password = f"dummy={token_hex(6)}=="
    script, dump = restore_tree(tmp_path, dotenv=f"POSTGRES_USER=duka\nPOSTGRES_PASSWORD={password}\n")
    run = run_stubbed(tmp_path, script, dump, env={})
    assert run.returncode == 0, run.output
    assert run.database_url.startswith(f"postgresql+psycopg://duka:{password}@db:5432/duka_verify_")
    assert password not in run.argv and password not in run.output
