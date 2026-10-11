"""Operator scripts never put a secret on a command line, where every local user can read it in the process list.
Values reach `docker compose run` / `docker run` by name (`-e VAR`), taken from the script's environment, or from an
env file (scripts/verify_restore.sh)."""
import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|API_KEY|DATABASE_URL", re.I)
# `-e NAME=…`, `--env NAME=…`, `--env=NAME=…`, the pair optionally quoted; never `--env-file …` or `-e NAME` alone.
ENV_WITH_VALUE = re.compile(r"(?:^|\s)(?:-e\s+|--env(?:\s+|=))[\"']?([A-Za-z_][A-Za-z0-9_]*)=")


def secret_values(text: str) -> list[str]:
    return [f"{n}: {name}" for n, line in enumerate(text.splitlines(), 1)
            for name in ENV_WITH_VALUE.findall(line) if SECRET_NAME.search(name)]


@pytest.mark.parametrize("line", [
    '-e DATABASE_URL="postgresql+psycopg://u:$PW@db:5432/x"',
    '-e VERIFY_PASSWORD="$VERIFY_PASSWORD" backend',
    "--env POSTGRES_PASSWORD=$DB_PASSWORD",
    "docker run --env=OPENAI_API_KEY=$KEY image",
    '-e "JWT_SECRET=$JWT"',
    "-e 'OPS_TOKEN=abc'",
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
])
def test_names_env_files_and_plain_values_are_allowed(line):
    assert secret_values(line) == []


def test_no_operator_script_passes_a_secret_as_a_command_line_value():
    if not SCRIPTS.is_dir():
        pytest.skip("scripts/ is not part of this checkout (the backend image holds backend/ only)")
    scripts = sorted(SCRIPTS.glob("*.sh"))
    assert any(p.name == "verify_restore.sh" for p in scripts)
    found = {p.name: secret_values(p.read_text(encoding="utf-8")) for p in scripts}
    assert {name: hits for name, hits in found.items() if hits} == {}
