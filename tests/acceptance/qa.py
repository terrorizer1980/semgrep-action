import os
import pty
import re
import subprocess
import sys
import tempfile
from functools import reduce
from pathlib import Path
from typing import Any
from typing import Callable
from typing import cast
from typing import Mapping
from typing import Optional
from typing import Sequence

from _pytest.python import Metafunc
from ruamel import yaml

ALL_TESTS = [
    d.name
    for d in Path(__file__).parent.resolve().iterdir()
    if d.is_dir() and (d / "commands.yaml").exists()
]

REPO_ROOT = str(Path(__file__).parent.parent.parent.resolve())

BRANCH_COMMIT = re.compile(r"^(commit|\|   \*) ([0-9a-f]+)", re.MULTILINE)
DATE_STR = re.compile(r"^Date:   (.*)$", re.MULTILINE)
ENV_VERSIONS = re.compile(
    r"^(\| versions\s+?- semgrep ).+?( on Python ).+?$", re.MULTILINE
)
GITHUB_ACTIONS_DEBUG = re.compile(r"^::debug::.+?\n", re.MULTILINE)

PIPE_OUTPUT: Mapping[str, Callable[[subprocess.CompletedProcess], str]] = {
    "expected_out": lambda r: cast(str, r.stdout),
    "expected_err": lambda r: cast(str, r.stderr),
}
SYMLINK_SKIP_MSG = re.compile(
    r"Skipping .*bar/foo since it is a symlink to a directory: .*foo", re.MULTILINE
)
JSON_OUTPUT_COMMIT_DATE = re.compile(
    r'"commit_date": "\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+]\d{2}:\d{2}"'
)


def write_expected_file(filename: str, output: str) -> None:
    with open(filename, "w") as file:
        file.write(clean_output(output))


CLEANING_FUNCS: Sequence[Callable[[str], str]] = [
    lambda s: "\n".join(line.rstrip() for line in s.splitlines()) + "\n",
    lambda s: re.sub(BRANCH_COMMIT, r"\1 aaaaaaa", s),
    lambda s: re.sub(DATE_STR, r"Date:   YYYY-MM-DD", s),
    lambda s: re.sub(ENV_VERSIONS, r"\1x.y.z\2x.y.z", s),
    lambda s: re.sub(GITHUB_ACTIONS_DEBUG, "", s),
    lambda s: re.sub(
        SYMLINK_SKIP_MSG,
        "Skipping bar/foo since it is a symlink to a directory: foo",
        s,
    ),
    lambda s: re.sub(
        JSON_OUTPUT_COMMIT_DATE,
        '"commit_date": "0000-00-00T00:00:00+00:00"',
        s,
    ),
]


def clean_output(output: str) -> str:
    for clean in CLEANING_FUNCS:
        output = clean(output)
    return output


def match_expected(output: str, expected: str) -> bool:
    """Checks that OUTPUT matches EXPECTED

    Checks that OUTPUT and EXPECTED are exact
    matches ignoring trailing whitespace

    """
    output = clean_output(output)

    if output.strip() != expected.strip():
        print("==== EXPECTED ====")
        print(expected)
        print("==== ACTUAL ====")
        print(output)
    return output.strip() == expected.strip()


def check_command(step: Any, pwd: str, target: str, rewrite: bool) -> None:
    """Runs COMMAND in with cwd=PWD and checks that the returncode, stdout, and stderr
    match their respective expected values.

    If rewrite is True, overwrites expected files with output of running step, skipping
    output match verification
    """
    command = step["command"]
    if isinstance(command, str):
        command = command.split(" ")

    test_identifier = f"Target:{target} Step:{step['name']}"
    env = os.environ.copy()
    substituted = [part.replace("__REPO_ROOT__", REPO_ROOT) for part in command]

    print(f"======= {test_identifier} ========")

    # In order to test behavior with a stdin, we create a pseudoterminal for the command here.
    # To test behavior _without_ a stdin, set the "command" as:
    # command:
    #   - bash
    #   - -c
    #   - ": | <command>"
    master_fd, slave_fd = pty.openpty()

    runned = subprocess.run(
        substituted,
        cwd=pwd,
        env=env,
        stdin=slave_fd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )

    print("Command return code:", runned.returncode)

    if "returncode" in step:
        expected_returncode = step["returncode"]
        if runned.returncode != expected_returncode:
            print(f"Run stdout: {runned.stdout}")
            print(f"Run stderr: {runned.stderr}")
        assert runned.returncode == expected_returncode, test_identifier

    for pipe in ["expected_out", "expected_err"]:
        if pipe in step:
            expectation_file = step.get(pipe)
            if rewrite and expectation_file is not None:
                write_expected_file(
                    f"tests/acceptance/{target}/{expectation_file}",
                    PIPE_OUTPUT[pipe](runned),
                )
            else:
                if expectation_file is None:
                    expectation = ""
                else:
                    with open(f"tests/acceptance/{target}/{expectation_file}") as file:
                        expectation = file.read()

                assert match_expected(
                    PIPE_OUTPUT[pipe](runned), expectation
                ), f"{test_identifier}: {pipe}"


def expand_include(step: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    include = step["include"]
    with open(f"tests/acceptance/{include}") as file:
        return cast(Sequence[Mapping[str, Any]], yaml.safe_load(file))


def run_repo(
    target: str, pre: Optional[Callable[[Path], None]] = None, rewrite: bool = False
) -> None:
    """
    Runs commands for a repository definition file.

    :param target: Subdirectory where the repository's commands are stored
    :param pre: A setup function to run after the repository is checked out, but prior to running commands
    """
    with open(f"tests/acceptance/{target}/commands.yaml") as file:
        info = yaml.safe_load(file)

    target_repo = info.get("target_repo")
    target_hash = info.get("target_hash")
    steps = info["steps"]
    steps = [i for s in steps for i in (expand_include(s) if "include" in s else [s])]

    with tempfile.TemporaryDirectory() as target_dir:

        if target_repo:
            subprocess.run(
                ["git", "clone", target_repo, target_dir],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            subprocess.run(
                ["git", "checkout", target_hash],
                cwd=target_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            subprocess.run(
                ["git", "clean", "-xdf"],
                cwd=target_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

        if pre:
            pre(Path(target_dir))

        for step in steps:
            check_command(step, target_dir, target, rewrite)


def pytest_generate_tests(metafunc: Metafunc) -> None:
    metafunc.parametrize("repo", ALL_TESTS)


def test_repo(repo: str) -> None:
    run_repo(repo)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        to_run = sys.argv[1:]
    else:
        to_run = ALL_TESTS

    for t in to_run:
        run_repo(t, rewrite=True)
