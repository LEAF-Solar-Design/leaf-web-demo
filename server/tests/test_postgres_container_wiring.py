"""Static checks for staged PostgreSQL container wiring.

These checks do not need a Docker daemon or a live database. They protect the
legacy defaults and process trust boundaries that must hold before an operator
can run the separate migration and cutover stages.
"""
from pathlib import Path, PurePosixPath
import ast
import json
import posixpath
import re
import shlex

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CUSTOMIZATION_POSTGRES_GATE_FRAGMENTS = (
    "- 'contract/customization.v1.schema.json'",
    "- 'platform/authority-inventory.json'",
    "- 'platform/db.py'",
    "- 'platform/migrations/0020_customization_authority.sql'",
    "- 'platform/tests/test_db_readiness_static.py'",
    "- 'scripts/reconcile_customization_authority.py'",
    "- 'scripts/reconcile_sessions_authority.py'",
    "- 'server/tests/test_reconcile_sessions_authority.py'",
    "- 'server/session_store.py'",
    "- 'server/customization_audit.py'",
    "- 'server/customization_authority.py'",
    "- 'server/customization_flags.py'",
    "- 'server/customization_models.py'",
    "- 'server/customization_postgres_store.py'",
    "- 'server/customization_service.py'",
    "- 'server/customization_store.py'",
    "- 'server/platform_link.py'",
    "- 'server/tests/test_customization_postgres_contract.py'",
    "- 'server/tests/test_customization_postgres_integration.py'",
    "- 'server/tests/test_customization_runtime.py'",
    "- 'server/tests/test_postgres_authority_inventory_contract.py'",
    (
        "PG_CUSTOMIZATION_TEST_URL: "
        "postgresql://postgres:postgres@127.0.0.1:5432/leaf_test"
    ),
    'test -n "${PG_CUSTOMIZATION_TEST_URL:-}"',
    'ln -s "$GITHUB_WORKSPACE/scripts"',
    'PYTHONPATH="$RUNNER_TEMP/leaf-customization-pythonpath"',
    '"$GITHUB_WORKSPACE/server/tests/test_customization_postgres_contract.py"',
    '"$GITHUB_WORKSPACE/server/tests/test_customization_postgres_integration.py"',
    '"$GITHUB_WORKSPACE/server/tests/test_customization_runtime.py"',
)


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _service(compose: str, name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\s*\n(.*?)(?=^  [a-zA-Z0-9_-]+:\s*\n|\Z)",
        compose,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing compose service {name}"
    return match.group(1)


def _required_environment(path: str) -> set[str]:
    manifest = json.loads(_read(path))
    return set(manifest["required"]["environment"])


def _assert_customization_postgres_gate(workflow: str) -> None:
    missing = [
        fragment
        for fragment in CUSTOMIZATION_POSTGRES_GATE_FRAGMENTS
        if fragment not in workflow
    ]
    assert not missing, f"customization PostgreSQL gate omitted: {missing}"


def _app_dockerfile() -> str:
    return _read("deploy/Dockerfile.app")


# Reading a Dockerfile by hand is the part of this guard that kept being wrong:
# across thirteen review findings, EVERY ONE was in this parse and none was in
# the contract it protects. So the guard was rebuilt to need it as little as
# possible, and what survives here is deliberately bounded.
#
# What was DELETED, and why, so nobody restores it thinking it was an oversight:
#
#   * `_final_workdir`. It gated no assertion. The guard requires an ABSOLUTE
#     path, which is correct whatever the WORKDIR is, so nothing ever needed to
#     know what the WORKDIR was; it only appeared in a failure message. Three
#     findings (keyword case, `\`-continuations, heredoc bodies) were fixes to
#     a component that decided nothing.
#   * The post-COPY survival check, which asked whether a later instruction
#     deletes a copied script. It could not be made correct. It matched the
#     literal string `/app/scripts`, so `RUN rm -rf scripts` in the region
#     where WORKDIR is still `/app` deleted both reconcilers and passed; so did
#     `./scripts`; so did a heredoc body line beginning with the word `copy`,
#     which the COPY exemption swallowed whole. Closing those needs the WORKDIR
#     in effect AT EACH LINE, i.e. an actual Dockerfile interpreter, and a
#     check that catches some spellings of a destruction while silently missing
#     others is worse than no check, because its green manufactures confidence.
#     The right home for that class is a runtime assertion in the image itself,
#     not a static test. Tracked as follow-up, deliberately not re-attempted.
#
# What REMAINS parses only COPY, and every way it can be wrong is LOUD: a COPY
# form it fails to understand empties the map, and an unmapped script then
# raises "no COPY ... was found" rather than passing quietly.
_DOCKERFILE_INSTRUCTION = re.compile(r"^([A-Za-z]+)\s+(\S.*)$")
_ESCAPE_DIRECTIVE = re.compile(r"^#\s*escape\s*=", re.IGNORECASE)
_HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")


def _logical_lines(dockerfile: str) -> list[str]:
    """Instruction lines: continuations joined, comments and heredocs dropped."""
    # The `escape` parser directive can change the continuation character, which
    # would silently invalidate the joining below. Fail closed instead of
    # guessing; deploy/Dockerfile.app does not use one.
    assert not any(
        _ESCAPE_DIRECTIVE.match(line.strip()) for line in dockerfile.splitlines()
    ), "Dockerfile sets an escape directive; this parser assumes a backslash"

    raw_lines = dockerfile.splitlines()
    lines: list[str] = []
    index = 0
    while index < len(raw_lines):
        pending = raw_lines[index].strip()
        index += 1
        if not pending or pending.startswith("#"):
            continue

        while pending.endswith("\\") and index < len(raw_lines):
            pending = pending[:-1].rstrip()
            nxt = raw_lines[index].strip()
            index += 1
            # A comment or blank line inside a continuation is removed and does
            # NOT end it, so put the marker back and keep consuming.
            if not nxt or nxt.startswith("#"):
                pending = f"{pending} \\"
                continue
            pending = f"{pending} {nxt}"
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()

        # Heredoc BODIES belong to the instruction, not to the file. Docker
        # keeps every line up to the delimiter inside e.g. `RUN <<EOF`, so a
        # body line reading `WORKDIR /app` is not a WORKDIR at all — treating
        # it as one is how this guard failed open a third time.
        for delimiter in _HEREDOC.findall(pending):
            while index < len(raw_lines) and raw_lines[index].strip() != delimiter:
                index += 1
            index += 1  # drop the delimiter line itself

        lines.append(pending)
    return lines


# The info string after the backticks is not just a language: markdown lets it
# carry attributes, and nothing forbids indenting the fence or capitalising it.
# The exact-spelling version silently skipped ` ```shell ` (one trailing space),
# ` ```shell title=x ` and ` ```SHELL `, so a NEW worked example spelled any of
# those ways would go unchecked while the anti-vacuity count stayed green.
#
# The tag must END here, and `\b` is not enough to say so: `\b` is satisfied by
# the hyphen in ```shell-session, so a TRANSCRIPT fence -- whose lines carry a
# `$ ` prompt and quote commands as a user typed them, relative paths included --
# was read as a list of runnable commands and then failed for using one. That is
# a false positive on ordinary documentation. `(?![\w-])` ends the tag at a real
# boundary, so ```shell-session, ```sh-session and ```bash-repl stay unread while
# ```shell title=x is still read.
_SHELL_FENCE = re.compile(
    r"^[ \t]*```[ \t]*(?:shell|bash|sh)(?![\w-])[^\n]*\n(.*?)^[ \t]*```",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


def _command_tokens(command: str, origin: str) -> list[str]:
    """Tokenise as the operator's shell will, not with a naive `.split()`.

    `.split()` reads a QUOTED path as a distinct token that no longer matches
    a bare script name, and reads the text of an inline `#` comment as real
    arguments. Both matter: a documented
        python 'scripts/reconcile_x.py' --mode parity  # /app/scripts/reconcile_x.py
    passed a `.split()` guard, because the quoted relative path was skipped
    while the absolute path in the comment was counted as the real one. The
    shell strips the quotes and drops the comment, so the operator runs the
    relative form and hits the missing path.
    """
    try:
        return shlex.split(command, comments=True, posix=True)
    except ValueError as exc:
        raise AssertionError(
            f"{origin} is not parseable as a shell command ({exc}), so this "
            f"guard cannot tell what it runs: {command!r}"
        ) from exc


def _assert_not_a_near_miss(origin: str, token: str, repo_scripts: set[str]) -> None:
    """Refuse a token that MEANT a repository script but does not name one.

    Selection is exact, so a near miss is invisible -- and a near miss IS the
    defect, not a step away from it. A documented command naming a script that
    does not exist under that exact spelling fails for the same operator, in the
    same image, as the relative-path defect this guard was built for; it just
    fails with "No such file or directory" instead. Without this, such a token
    is dropped silently: never asserted absolute, never compared to the COPY
    target, and never counted, so the anti-vacuity floor still sees eight and
    the whole guard reports green over a command that cannot run.

    Two independent nets, because each catches what the other cannot.
    """
    name = PurePosixPath(token).name
    # The image filesystem is case-SENSITIVE while the authoring machine's
    # usually is not, so a capital slip that resolves fine locally -- and that
    # `Path.exists()` would confirm on Windows or macOS -- is a missing file in
    # the container.
    actual = {script.lower(): script for script in repo_scripts}.get(name.lower())
    assert actual is None, (
        f"{origin} names {token!r}, but the repository file is scripts/{actual} "
        f"-- the same name in different case. The image filesystem is "
        f"case-sensitive, so this command finds no such file in the only place "
        f"it is ever run."
    )
    # A plain typo keeps its case, so case alone cannot catch it. Any .py named
    # inside a scripts/ directory has to be one of ours: there is nowhere else
    # such a token could legitimately point.
    assert not (
        name.endswith(".py") and PurePosixPath(token).parent.name == "scripts"
    ), (
        f"{origin} names {token!r}, a .py under a scripts/ directory that this "
        f"repository does not have. Either it is a typo, or the script was "
        f"renamed and this command was not. Repository scripts/: "
        f"{sorted(repo_scripts)}"
    )


def _documented_shell_commands(markdown: str) -> list[str]:
    """Runnable commands from ```shell fences, `\\`-continuations joined.

    FENCED BLOCKS ONLY, deliberately. The prose around them quotes the WRONG
    (repository-relative) form on purpose to explain why the right one is
    absolute, and treating that explanation as a command would make the guard
    cry wolf about its own documentation.
    """
    commands: list[str] = []
    for block in _SHELL_FENCE.findall(markdown):
        pending = ""
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            pending = f"{pending} {stripped}".strip() if pending else stripped
            if pending.endswith("\\"):
                pending = pending[:-1].rstrip()
                continue
            commands.append(pending)
            pending = ""
        if pending:
            commands.append(pending)
    return commands


def _single_stage(dockerfile: str) -> None:
    """Refuse to reason about a multi-stage build instead of guessing wrong.

    `_copied_scripts` folds every instruction together, but
    each `FROM` starts a fresh stage: a later stage inherits nothing unless it
    copies it, so a COPY in an earlier stage says nothing about the published
    image. Rather than re-implement stage inheritance, fail closed and make a
    human re-examine this guard when a stage is added. deploy/Dockerfile.app
    has exactly one stage today.
    """
    stages = [argument for keyword, argument in _instructions(dockerfile)
              if keyword == "FROM"]
    assert len(stages) == 1, (
        f"deploy/Dockerfile.app now has {len(stages)} stages ({stages}). This "
        "guard folds all instructions together and cannot tell which stage "
        "reaches the published image. Teach it stage inheritance before "
        "relaxing this assertion."
    )


def _instructions(dockerfile: str) -> list[tuple[str, str]]:
    """(KEYWORD, argument-text) for each instruction, keyword upper-cased."""
    parsed: list[tuple[str, str]] = []
    for line in _logical_lines(dockerfile):
        match = _DOCKERFILE_INSTRUCTION.match(line)
        if match:
            parsed.append((match.group(1).upper(), match.group(2).strip()))
    return parsed


def _image_path(destination: str) -> str:
    """The path the image really holds, from a COPY/ADD destination as written.

    THE ASYMMETRY HERE IS THE POINT, and getting it backwards is what let a
    COPY land on a tracked script in silence. The COMMAND side is never
    normalized -- the operator's shell takes `/app/scripts/x.py/` literally and
    the kernel refuses it with ENOTDIR -- but the DESTINATION side must be,
    because docker resolves `.`, `//` and `..` lexically before it writes. So
    `/app/scripts/./x.py` and `/app/scripts/x.py` are two spellings of ONE file
    in the image, and comparing them as raw strings said they were different.
    Measured against docker: every dotted, doubled and traversing spelling of a
    tracked script's path escaped the lands-on check below.
    """
    resolved = posixpath.normpath(destination)
    # POSIX leaves a LEADING exactly-double slash implementation-defined, and
    # normpath preserves it; Linux does not, so `//app/scripts/x.py` is the
    # same file as `/app/scripts/x.py` and must compare equal to it.
    if resolved.startswith("//") and not resolved.startswith("///"):
        resolved = resolved[1:]
    return resolved


def _copy_operands(keyword: str, argument: str) -> tuple[list[str], list[str], str, bool]:
    """(flags, sources, normalized destination, destination-is-a-directory).

    Reads BOTH `COPY` and `ADD`: they write to the image identically, and
    reading only COPY meant `ADD data/blob /app/scripts/reconcile_x.py` shipped
    other content under a documented script's path with the guard silent.
    """
    # The JSON ("exec") form is a different grammar. `.split()` half-reads it --
    # `COPY ["a", "/app/scripts/x.py"]` yields a destination still wearing its
    # bracket and quote, which then matches nothing -- so the instruction slid
    # past the lands-on check looking like a checked one. Refuse it loudly
    # instead; deploy/Dockerfile.app does not use it.
    assert not argument.lstrip().startswith("["), (
        f"{keyword} uses the JSON form ({argument!r}); this guard reads the "
        "shell form only, and half-reading this one hides its destination. "
        "Teach this guard before using that form."
    )
    fields = argument.split()
    flags = [field for field in fields if field.startswith("--")]
    # COPY takes flags before its operands (`--chown=`, `--from=`, `--link=`,
    # ...). Requiring exactly two fields silently skipped every flagged form,
    # which is how a script could be shipped and never checked. Split flags off
    # rather than counting fields.
    operands = [field for field in fields if not field.startswith("--")]
    assert len(operands) >= 2, (
        f"{keyword} {argument!r} has fewer than two operands, which docker "
        "rejects; this guard cannot tell what it writes."
    )
    destination = operands[-1]
    sources = operands[:-1]
    # REFUSE what this guard cannot resolve, rather than resolving it wrongly.
    #
    # Docker builds a COPY destination in ways this parser deliberately does not
    # model: it expands ENV and ARG variables, and it resolves a RELATIVE
    # destination against the WORKDIR in effect at that line. So
    #     ENV TARGET=/app/scripts   +   COPY data/blob $TARGET/x.py
    #     WORKDIR /app              +   COPY data/blob scripts/x.py
    # both write /app/scripts/x.py, and both compared unequal to it as strings,
    # so each slipped the lands-on check in silence.
    #
    # NOTE FOR ANYONE TEMPTED TO FIX THIS BY TRACKING WORKDIR: that component
    # existed, was wrong three times, and was deleted on purpose (see the note
    # above `_DOCKERFILE_INSTRUCTION`). It gated no assertion and every one of
    # its bugs failed OPEN. This is the opposite move: it does not compute the
    # WORKDIR, it declines to accept any line whose meaning depends on knowing
    # it. Refusing is sound with no interpreter; resolving is not.
    assert "$" not in destination and "$" not in " ".join(sources), (
        f"{keyword} {argument!r} builds a path from a variable, which docker "
        "expands and this guard does not. Teach this guard before using that "
        "form, or write the path literally."
    )
    assert destination.startswith("/"), (
        f"{keyword} {argument!r} has a RELATIVE destination, which docker "
        f"resolves against the WORKDIR in effect at that line. This guard does "
        f"not track the WORKDIR -- on purpose -- so it cannot tell where "
        f"{destination!r} lands. Write the destination absolutely."
    )
    # A trailing slash is the ONE thing that must be read before normalizing,
    # because it is what distinguishes "write this file" from "write into this
    # directory", and normpath drops it.
    is_directory = destination.endswith("/") or len(sources) > 1
    assert len(sources) == 1 or destination.endswith("/"), (
        f"{keyword} {argument!r} has several sources but its destination is "
        "not written as a directory; docker requires a trailing slash there."
    )
    return flags, sources, _image_path(destination), is_directory


def _copied_scripts(dockerfile: str) -> dict[str, str]:
    """basename -> absolute in-image path, for each `scripts/...` file shipped.

    Deliberately restricted to explicit file copies out of scripts/. Directory
    copies such as `COPY server/ /app/server/` are a different shape, and the
    pytest/npm parity commands in the inventory are documented for a source
    checkout rather than for the image, so keying on these lines is what keeps
    this guard aimed at the commands that really do run in the container.
    """
    copies: dict[str, str] = {}
    destinations: list[tuple[str, str, bool]] = []
    for keyword, argument in _instructions(dockerfile):
        if keyword not in ("COPY", "ADD"):
            continue
        flags, sources, destination, is_directory = _copy_operands(keyword, argument)
        for source in sources:
            destinations.append((source, destination, is_directory))
            if not source.startswith("scripts/"):
                continue
            # `--from=` sources another stage or an external image, so the path
            # is not this repository's scripts/ tree and the reasoning below
            # does not hold. Refuse rather than assume.
            assert not any(flag.startswith("--from=") for flag in flags), (
                f"{keyword} of {source} uses --from=, so it does not come from "
                "the repository tree; teach this guard before using that form."
            )
            # KEY ON THE REPOSITORY SOURCE, NOT THE DESTINATION. Keying on the
            # destination basename asked only "does something land at this
            # name", never "is it the right file". A one-token slip such as
            #     COPY scripts/broker-container-smoke.py /app/scripts/reconcile_x.py
            # builds fine and satisfies a destination-keyed map, while the
            # operator running the documented arguments gets `unrecognized
            # arguments` from whatever script actually shipped under that name.
            source_name = PurePosixPath(source).name
            if is_directory:
                # `COPY scripts/x.py /app/scripts/` is a DIRECTORY destination,
                # which docker resolves to /app/scripts/x.py. Reading its
                # basename as the shipped name called that a rename and refused
                # a correct, ordinary line.
                landing = posixpath.join(destination, source_name)
            else:
                landing = destination
                assert PurePosixPath(landing).name == source_name, (
                    f"{keyword} ships {source} as {landing}, renaming it. This "
                    f"guard identifies a shipped script by its repository name, "
                    f"so a rename means the image holds one script's content "
                    f"under another's documented name. Teach this guard before "
                    f"using that form."
                )
            # Carried over from the sessions-only guard #495 merged: two COPYs
            # of one basename make "the" image path ambiguous, and a dict would
            # silently keep the last one.
            assert source_name not in copies, (
                f"{source_name} is COPYed to more than one target"
            )
            copies[source_name] = landing

    # A COPY does not have to come FROM scripts/ to land ON a tracked script.
    # `COPY data/rooftop_demo.intake.json /app/scripts/reconcile_x.py` has a
    # non-scripts source, so the loop above skips it entirely, while in the
    # image it replaces the tracked script's content at the documented path and
    # the operator's command runs the wrong file. Same for a directory copy
    # landing on the scripts directory. Checked here, on DESTINATIONS, because
    # this needs no WORKDIR reasoning: a COPY destination is absolute and
    # literal, unlike a RUN's shell, which is why this class is checkable and
    # the post-COPY-deletion class was not.
    #
    # EVERY ARITY, not just two operands. A multi-source `COPY data/a.py
    # data/b.json /app/scripts/` was dropped before it was ever recorded as a
    # destination, and docker merges it straight over the scripts directory --
    # built and confirmed against the real daemon, not merely read.
    for target in copies.values():
        parent = posixpath.dirname(target)
        for source, destination, _ in destinations:
            if source.startswith("scripts/"):
                continue
            assert destination not in (target, parent), (
                f"COPY writes {source} to {destination}, which lands on "
                f"{target}. The image would ship that content under a "
                f"documented script's path, so the operator's command runs the "
                f"wrong file. This guard identifies a script by its repository "
                f"source; teach it before overwriting a tracked destination."
            )
    return copies


def test_dockerfile_copy_parsing_survives_case_indentation_and_heredocs():
    """The COPY map is only as good as this parse, so pin the parse itself.

    Scope note: this used to pin WORKDIR parsing too. That went with
    `_final_workdir`, which gated no assertion -- the guard requires an
    ABSOLUTE path, which is correct whatever the WORKDIR is, so nothing needed
    to know what the WORKDIR was. What remains protects `_copied_scripts`,
    whose failures are LOUD (a missed COPY raises "no COPY ... was found"),
    never silent.
    """
    dockerfile = (
        "# a comment mentioning COPY scripts/decoy.py /app/scripts/decoy.py\n"
        "FROM python:3.12-slim\n"
        "  copy scripts/reconcile_demo.py /app/scripts/reconcile_demo.py\n"
        "ENV APP_PORT=8130 \\\n"
        "    PYTHONUNBUFFERED=1\n"
    )

    # Case-insensitive, indented, and a commented decoy must not register.
    assert _copied_scripts(dockerfile) == {
        "reconcile_demo.py": "/app/scripts/reconcile_demo.py"
    }
    # A continuation line must never be read as an instruction named APP.
    assert "APP" not in {keyword for keyword, _ in _instructions(dockerfile)}
    # A directory copy is a different shape and must stay out of the mapping.
    assert _copied_scripts("COPY server/ /app/server/\n") == {}
    # A flagged COPY still registers; counting fields once missed these.
    assert _copied_scripts(
        "COPY --chown=10001:10001 scripts/x.py /app/scripts/x.py\n"
    ) == {"x.py": "/app/scripts/x.py"}

    # The real Dockerfile contains this shape: HEALTHCHECK continues onto a CMD
    # line, which a per-line parse reports as a separate CMD instruction.
    health = (
        "HEALTHCHECK --interval=10s \\\n"
        '  CMD python -c "import urllib.request"\n'
    )
    assert [keyword for keyword, _ in _instructions(health)] == ["HEALTHCHECK"]

    # An escape directive would change the continuation character, so the
    # parser must refuse rather than silently mis-join.
    with pytest.raises(AssertionError, match="escape directive"):
        _instructions("# escape=`\nCOPY scripts/x.py /app/scripts/x.py\n")

    # A heredoc BODY belongs to its instruction, so a COPY faked inside one is
    # not a COPY.
    assert _copied_scripts(
        "RUN <<EOF\nCOPY scripts/fake.py /app/scripts/fake.py\nEOF\n"
    ) == {}

    # A renaming COPY is refused rather than mapped, and so is --from=.
    with pytest.raises(AssertionError, match="renaming it"):
        _copied_scripts("COPY scripts/a.py /app/scripts/b.py\n")
    with pytest.raises(AssertionError, match="--from="):
        _copied_scripts("COPY --from=build scripts/x.py /app/scripts/x.py\n")

    # A COPY need not come FROM scripts/ to land ON a tracked script. Both the
    # file form and the directory form must be refused, or the image ships
    # other content under a documented script's path.
    #
    # EVERY ONE of the spellings below was measured GREEN against the merged
    # guard, and two of them were built against a real docker daemon and the
    # overwritten file read back out of the image. They are pinned individually
    # rather than as one case because they escaped for THREE different reasons:
    # an arity the parse dropped, a spelling the kernel resolves and a raw
    # string comparison did not, and an instruction keyword never read at all.
    shipped = "COPY scripts/x.py /app/scripts/x.py\n"
    for escape in (
        # ...the two the merged fix already caught, kept as the control.
        "COPY data/blob.json /app/scripts/x.py\n",
        "COPY vendor/ /app/scripts/\n",
        # ...arity: three operands were dropped before being recorded at all.
        "COPY data/a.py data/b.json /app/scripts/\n",
        "COPY --chown=1:1 data/a.py data/b.json /app/scripts/\n",
        # ...spelling: docker resolves these, raw string comparison did not.
        "COPY data/blob /app/scripts/./x.py\n",
        "COPY data/blob //app/scripts/x.py\n",
        "COPY data/blob /app/server/../scripts/x.py\n",
        "COPY data/blob /app/scripts//x.py\n",
        # ...keyword: ADD writes to the image exactly as COPY does.
        "ADD data/blob /app/scripts/x.py\n",
        "ADD data/blob /app/scripts/\n",
    ):
        with pytest.raises(AssertionError, match="lands on"):
            _copied_scripts(shipped + escape)

    # The JSON form is a different grammar, and `.split()` half-read it into a
    # destination wearing a bracket that matched nothing. Refuse, do not guess.
    with pytest.raises(AssertionError, match="JSON form"):
        _copied_scripts(shipped + 'COPY ["data/blob", "/app/scripts/x.py"]\n')

    # A destination docker BUILDS rather than reads literally must be refused,
    # not resolved. Both of these write /app/scripts/x.py in the image and both
    # compared unequal to it as strings, so both landed on a tracked script in
    # silence. Refusing is sound without a Dockerfile interpreter; tracking the
    # WORKDIR to resolve them is the component that was deleted for failing open.
    for unresolvable in (
        "ENV TARGET=/app/scripts\nCOPY data/blob $TARGET/x.py\n",
        "ENV TARGET=/app/scripts\nCOPY data/blob ${TARGET}/x.py\n",
    ):
        with pytest.raises(AssertionError, match="from a variable"):
            _copied_scripts(shipped + unresolvable)
    for unresolvable in (
        "WORKDIR /app\nCOPY data/blob scripts/x.py\n",
        "WORKDIR /app\nCOPY vendor/ scripts/\n",
    ):
        with pytest.raises(AssertionError, match="RELATIVE destination"):
            _copied_scripts(shipped + unresolvable)

    # A DIRECTORY destination is not a rename: docker resolves
    # `COPY scripts/x.py /app/scripts/` to /app/scripts/x.py. Refusing it was a
    # false positive that would have blocked an ordinary, correct line.
    assert _copied_scripts("COPY scripts/x.py /app/scripts/\n") == {
        "x.py": "/app/scripts/x.py"
    }
    # ...and the multi-source form of the same thing maps every source.
    assert _copied_scripts("COPY scripts/a.py scripts/b.py /app/scripts/\n") == {
        "a.py": "/app/scripts/a.py",
        "b.py": "/app/scripts/b.py",
    }
    # A destination spelled oddly still resolves to the one path the image
    # holds, so a documented command naming that path must still match it.
    assert _copied_scripts("COPY scripts/x.py /app/scripts/./x.py\n") == {
        "x.py": "/app/scripts/x.py"
    }

    # Multi-stage is refused rather than guessed at.
    _single_stage("FROM python:3.12-slim AS app\nWORKDIR /app\n")
    with pytest.raises(AssertionError, match="now has 2 stages"):
        _single_stage("FROM python AS build\nFROM python AS app\n")


def test_a_documented_command_that_names_no_real_script_is_not_skipped():
    """The selection step must fail loudly on a near miss, not drop it.

    This is the hole the anti-vacuity count cannot cover. The count is 8 because
    there are 8 commands: ADD a ninth with a typo'd script name and it is
    neither checked nor counted, so every assertion in the guard stays green
    while the new documented command dies in the image. Both reconcilers are
    named explicitly further down, but naming proves the RIGHT commands are
    still reached -- it says nothing about a WRONG one arriving beside them.
    """
    scripts = {"reconcile_customization_authority.py", "reconcile_sessions_authority.py"}

    # A capital slip. Plausible precisely because it is invisible to the author:
    # Windows and macOS resolve it, the case-sensitive image does not.
    with pytest.raises(AssertionError, match="different case"):
        _assert_not_a_near_miss(
            "docs/POSTGRES-CUTOVER.md",
            "/app/scripts/Reconcile_customization_authority.py",
            scripts,
        )
    # A dropped letter. Same case, so the net above cannot see it.
    with pytest.raises(AssertionError, match="does not have"):
        _assert_not_a_near_miss(
            "docs/POSTGRES-CUTOVER.md",
            "/app/scripts/reconcil_customization_authority.py",
            scripts,
        )
    # A script that was renamed while its documented command was not.
    with pytest.raises(AssertionError, match="does not have"):
        _assert_not_a_near_miss(
            "authority-inventory.json x backfill", "scripts/old_name.py", scripts
        )

    # And it must NOT cry wolf. These tokens are not trying to be repo scripts,
    # and forcing them through the absolute-path rule would be wrong: the pytest
    # parity commands really are documented for a source checkout.
    for innocent in (
        "server/tests/test_jobs_callbacks_postgres.py",
        "test/sessionStoreFactory.test.ts",
        "docker-compose.canonical.yml",
        "--mode",
        "/data/state/customization.db",
        "scripts/compose.harness-smoke.yml",
    ):
        _assert_not_a_near_miss("authority-inventory.json x parity", innocent, scripts)


def test_a_shell_fence_is_found_however_it_is_spelled():
    """A worked example must not escape by how its fence is written.

    Selection from the document is the only reason the cutover doc is guarded at
    all, and the exact-spelling regex skipped four ordinary spellings in
    silence. A skipped fence does not fail: it just is not there, and the
    document's floor of two is already met by the fences that do parse.
    """
    body = "python /app/scripts/reconcile_customization_authority.py --mode parity\n"
    for fence in (
        "```shell",
        "```shell ",
        "```bash",
        "```sh",
        "```SHELL",
        "```shell title=cutover",
        "```shell {.highlight}",
    ):
        assert _documented_shell_commands(f"{fence}\n{body}```\n") == [body.strip()], fence

    # An indented fence is still a fence.
    assert _documented_shell_commands(f"  ```shell\n  {body}  ```\n") == [body.strip()]

    # A language this guard does not read stays unread, deliberately: the prose
    # around these blocks quotes the WRONG form on purpose to explain it.
    assert _documented_shell_commands(f"```text\n{body}```\n") == []
    assert _documented_shell_commands(f"```python\n{body}```\n") == []

    # A TRANSCRIPT is not a list of runnable commands. Its lines carry a `$ `
    # prompt and quote what a user typed, relative paths and all, so reading one
    # as commands makes this guard fail ordinary documentation. The tag must end
    # at a real boundary: `\b` is satisfied by the hyphen and let these through.
    transcript = "$ python scripts/reconcile_customization_authority.py --mode parity\n"
    for tag in ("shell-session", "sh-session", "bash-repl", "shellscript"):
        assert _documented_shell_commands(f"```{tag}\n{transcript}```\n") == [], tag


def test_documented_authority_commands_resolve_in_the_image():
    """Every documented reconciler command must resolve to the file the image
    actually holds, for EVERY authority rather than one hand-picked entry.

    The class defect this closes: the repo pinned both halves of the contract
    independently and never compared them. One test asserted the
    `COPY ... /app/scripts/...` line, another asserted the command string, and
    nothing knew about WORKDIR. Both passed while the documented command
    resolved to /app/server/scripts/..., which does not exist, so it failed in
    the only place it is ever run. A per-authority copy of this check would
    re-open the same hole for the next authority, so this loops instead.

    ORDER MATTERS HERE. The primary assertion is that the path is ABSOLUTE,
    and it deliberately does not depend on the Dockerfile parse at all. Three
    review rounds found three ways to fool that parse -- case, continuations,
    heredocs -- and every one of them failed OPEN by mis-computing the WORKDIR
    and letting a relative path look correct. An absolute path is right
    regardless of WORKDIR, which is the whole property being bought, so
    requiring it first means no future parser hole can readmit the original
    defect. The COPY-target comparison is a second, weaker check on top.
    """
    dockerfile = _app_dockerfile()
    _single_stage(dockerfile)

    copies = _copied_scripts(dockerfile)
    assert copies, "deploy/Dockerfile.app copies no scripts/ file"

    # WHAT TO CHECK IS CHOSEN FROM THE REPOSITORY, NOT FROM THE DOCKERFILE.
    # Selecting on the COPY map made a parse gap invisible twice over: a script
    # the parse missed was neither checked NOR counted, so both anti-vacuity
    # assertions stayed green while a broken command shipped. Keying on "this
    # token names a real file in scripts/" cannot miss that way, whatever COPY
    # form the Dockerfile uses. Restricted to .py so that a future documented
    # `docker compose -f scripts/compose.harness-smoke.yml ...` is not forced
    # absolute and then reported as unshipped.
    repo_scripts = {
        path.name for path in (REPO_ROOT / "scripts").iterdir()
        if path.is_file() and path.suffix == ".py"
    }
    assert repo_scripts, "no .py files in scripts/"

    # EVERY PLACE A HUMAN COPIES THE COMMAND FROM, not just the inventory.
    # The inventory is the machine-readable source, but the cutover operator
    # follows docs/POSTGRES-CUTOVER.md, so a relative path there is the same
    # defect with the same reachable caller. Checking only one of the two left
    # the other free to rot.
    sources: list[tuple[str, str]] = []
    inventory = json.loads(_read("platform/authority-inventory.json"))
    for authority in inventory["authorities"]:
        for phase in ("backfill", "parity"):
            command = authority.get(phase, {}).get("command")
            if command:
                sources.append((f"authority-inventory.json {authority['id']} {phase}",
                                command))
    documented = _documented_shell_commands(_read("docs/POSTGRES-CUTOVER.md"))
    sources.extend(("docs/POSTGRES-CUTOVER.md", command) for command in documented)

    checked: list[tuple[str, str]] = []
    for origin, command in sources:
        for token in _command_tokens(command, origin):
            name = PurePosixPath(token).name
            if name not in repo_scripts:
                _assert_not_a_near_miss(origin, token, repo_scripts)
                continue

            # A script that is documented but that the Dockerfile is not seen
            # to copy is a blocker either way: either it does not ship and the
            # command cannot run, or the COPY parse missed the form used and
            # this guard is blind. Do not skip it.
            target = copies.get(name)
            assert target is not None, (
                f"{origin} names {token!r}, but no COPY of scripts/{name} into "
                f"the image was found in deploy/Dockerfile.app. Either the "
                f"script is not shipped, or this guard's COPY parse does not "
                f"understand the form used. Both are blockers."
            )

            # COMPARE THE TOKEN RAW, NEVER NORMALIZED. PurePosixPath quietly
            # rewrites what the shell would take literally: a trailing slash and
            # a `/./` segment both vanish, so `/app/scripts/x.py/` compared
            # equal while the kernel would refuse it with ENOTDIR (a trailing
            # slash demands a directory). Normalizing here means judging a
            # string the operator will never run.
            #
            # The DESTINATION side is the opposite case and is normalized in
            # `_image_path`: docker resolves the spelling before it writes, so
            # `target` is already the one path the image really holds. Raw on
            # the command side, resolved on the image side -- that asymmetry is
            # deliberate, and collapsing it either way reopens a hole.
            assert token.startswith("/"), (
                f"{origin} names {token!r} by a repository-relative path. The "
                f"image does NOT run from the repository root -- Dockerfile.app "
                f"ends on a WORKDIR under /app -- so a relative path resolves "
                f"somewhere the script is not, while the script is at {target}. "
                f"Use the absolute path: it is correct whatever the WORKDIR "
                f"turns out to be, which is exactly why this check does not "
                f"read the WORKDIR to decide."
            )
            assert token == target, (
                f"{origin} names {token!r}, but deploy/Dockerfile.app copies "
                f"that script to {target}. The command must name that path "
                f"exactly, character for character: the shell hands the token "
                f"to the kernel as written, and a trailing slash alone is "
                f"enough to fail with ENOTDIR even though it 'looks' right."
            )
            checked.append((origin, token))

    # Anti-vacuity: if either source's parse ever drifts, this test must go red
    # rather than silently check nothing. Six inventory commands across three
    # authority entries, plus both worked-example commands in the cutover doc.
    assert len(checked) >= 8, f"guard checked too few commands: {checked}"
    assert sum(1 for origin, _ in checked if origin.startswith("docs/")) >= 2, (
        "the cutover document's worked example was not reached; its shell "
        f"fences parsed to {documented}"
    )
    # NAME the reconcilers rather than trusting the count. A typo'd path drops
    # out of `repo_scripts` selection and goes unchecked, and today only the
    # count catches that, because 8 is exactly the number of commands there
    # are. Document a ninth and that protection would silently vanish. Naming
    # them costs two lines and does not decay.
    assert {
        "reconcile_customization_authority.py",
        "reconcile_sessions_authority.py",
    } <= {PurePosixPath(token).name for _, token in checked}, (
        "a known reconciler was not reached by any documented command; "
        f"reached={sorted({PurePosixPath(t).name for _, t in checked})}"
    )
    # Every script the image ships must be reached by a documented command.
    # This direction is now the only one keyed on the COPY map, and it is the
    # safe direction: a COPY the parse MISSES cannot hide a command here,
    # because selection above comes from the repository tree instead.
    assert set(copies).issubset({PurePosixPath(t).name for _, t in checked}), (
        "an image-copied reconciler script is documented by no command: "
        f"copied={sorted(copies)}, exercised={sorted(checked)}"
    )


def test_required_config_manifests_fail_closed_for_postgres_authority():
    app_environment = _required_environment("deploy/required-config.app.json")
    broker_environment = _required_environment("deploy/required-config.broker.json")

    assert {
        "LEAF_BLOB_STORE",
        "LEAF_DRAWING_MUTATIONS_FENCE_FILE",
        "LEAF_JOBS_STORE",
        "LEAF_PLATFORM_POSTGRES_REQUIRED",
        "LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED",
    }.issubset(app_environment)
    assert {
        "LEAF_BLOB_STORE",
        "LEAF_BROKER_STORE",
        "LEAF_DRAWING_MUTATIONS_FENCE_FILE",
    }.issubset(broker_environment)
    assert "LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED" not in broker_environment


def test_upload_import_boundary_has_a_real_postgres_pr_gate():
    workflow = _read(".github/workflows/upload-authority-postgres.yml")

    for protected_path in (
        "platform/api.py",
        # The shared cutover fence IS part of this boundary: without it listed,
        # a change to the fence alone would skip the gate entirely.
        "platform/mutation_fence.py",
        "platform/tests/test_drawing_import.py",
        "server/routers/uploads.py",
        "server/write_loop.py",
        "deploy/required-config.app.json",
    ):
        assert f"- '{protected_path}'" in workflow
    assert "working-directory: server" in workflow
    assert "python -m pytest --import-mode=importlib -q" in workflow
    assert "../platform/tests/test_drawing_import.py" in workflow


def test_live_dwg_version_restore_has_a_real_postgres_pr_gate():
    workflow = _read(".github/workflows/upload-authority-postgres.yml")

    assert "- 'server/tests/test_version_restore.py'" in workflow
    assert "Run PostgreSQL live-DWG version restore proof" in workflow
    assert "tests/test_version_restore.py" in workflow
    assert "postgres_live_dwg_restore_preserves_blob_and_readable_cache" in workflow


def test_customization_authority_has_a_real_postgres_pr_gate():
    _assert_customization_postgres_gate(
        _read(".github/workflows/upload-authority-postgres.yml")
    )


@pytest.mark.parametrize(
    "required_fragment",
    CUSTOMIZATION_POSTGRES_GATE_FRAGMENTS,
)
def test_customization_postgres_gate_rejects_each_omission(
    required_fragment: str,
):
    workflow = _read(".github/workflows/upload-authority-postgres.yml")
    assert required_fragment in workflow

    with pytest.raises(
        AssertionError,
        match="customization PostgreSQL gate omitted",
    ):
        _assert_customization_postgres_gate(
            workflow.replace(required_fragment, "", 1)
        )


def test_broker_image_contains_pg_runtime_without_crossing_secret_boundary():
    dockerfile = _read("deploy/Dockerfile.broker")
    dockerignore = _read(".dockerignore")

    assert "COPY platform/requirements.txt /app/platform/requirements.txt" in dockerfile
    assert "-r /app/platform/requirements.txt" in dockerfile
    assert "COPY platform/ /app/platform/" in dockerfile
    assert "LEAF_BROKER_STORE=legacy" in dockerfile
    assert "USER 10001:10001" in dockerfile

    # The broker may copy the one E2B helper, but never the harness manifest,
    # SDK dependency tree, grant files, or an APS credential.
    assert "COPY harness/package" not in dockerfile
    assert "COPY harness/src" not in dockerfile
    assert "COPY .aps" not in dockerfile
    assert "COPY .secrets" not in dockerfile
    assert "**/.env.local" in dockerignore
    assert ".secrets/" in dockerignore


def test_authored_tool_bodies_never_enter_the_build_context():
    """_legacy_author in server/routers/author.py writes tenant-authored Python to
    server/authored/<sha256(tenant_id)[:32]>/<tool>.py on a live request path, and
    both images COPY the whole server/ tree, so a body authored locally would ride
    into the image. Git excludes these only through a NESTED
    server/authored/.gitignore, which docker never reads, so the root
    .dockerignore has to carry the rule itself.
    """
    lines = [line.strip() for line in _read(".dockerignore").splitlines()]

    # Whole line, not a substring: a commented-out mention must not satisfy this.
    # The `**` is load-bearing -- a dockerignore `*` does not cross `/`, so a
    # narrowed `server/authored/*.py` would match only direct children and miss
    # the per-tenant depth the writer actually uses.
    assert "server/authored/**/*.py" in lines

    # Not a bare directory rule: .gitignore and .gitkeep under server/authored/
    # are tracked and are meant to keep reaching the build context.
    assert "server/authored/" not in lines

    # The rule only matters because the images copy the tree that holds it.
    for path in ("deploy/Dockerfile.app", "deploy/Dockerfile.broker"):
        assert re.search(r"^COPY server/\s+/app/server/", _read(path), flags=re.MULTILINE), path


# Each image's git install, pinned to the EXACT command it ships.
#
# Four review rounds of parsing the shell here produced four more false positives
# (`--simulate`, `-d`, `--only-upgrade`, a trailing `autoremove git`, `|| true`, a
# trailing `# git` comment, a quoted argument, install-then-purge) and four false
# negatives (`apt-get -y install git`, an install inside `if`/`for`, an environment
# prefix, `/usr/bin/apt-get`). A regex approximation of shell semantics cannot be
# made sound, and each round only moved the hole.
#
# So this pins the exact form instead. Nothing but these commands passes, which
# makes a false positive impossible rather than merely unlikely. Changing HOW an
# image installs git now requires editing this pin, and that is the point: it is a
# change a reviewer should see.
_PINNED_GIT_INSTALL = {
    "deploy/Dockerfile.app":
        "apt-get install -y --no-install-recommends git",
    "deploy/Dockerfile.broker":
        "apt-get install -y --no-install-recommends libstdc++6 git",
    "deploy/Dockerfile.harness":
        "apt-get install -y --no-install-recommends git ca-certificates",
}


def _shipped_stage(path: str) -> list[str]:
    """The final build stage's instructions, continuations joined, comments gone."""
    joined = re.sub(r"\\\s*\n\s*", " ", _read(path))
    lines = [ln for ln in joined.splitlines() if not re.match(r"\s*#", ln)]
    starts = [i for i, ln in enumerate(lines) if re.match(r"\s*FROM\s", ln)]
    return lines[starts[-1]:] if starts else lines


def _installs_git(path: str) -> bool:
    """Whether the shipped stage runs this image's pinned git install, unconditionally.

    Three checks, no shell interpretation: the exact pinned command appears in the
    shipped stage; it is not guarded by `||` so a failed install cannot pass; and
    nothing later removes git again.
    """
    pinned = _PINNED_GIT_INSTALL[path]
    for line in _shipped_stage(path):
        if not re.match(r"\s*RUN\s", line) or pinned not in line:
            continue
        before, _, _ = line.partition(pinned)
        if "||" in before:
            continue  # runs only if something else failed
        if re.search(r"\|\|\s*true", line):
            continue  # failure swallowed: the build succeeds without git
        if re.search(
            r"(apt-get|apt)\s+(purge|remove|autoremove)[^\n]*(?<![\w./-])git(?![\w./-])",
            line,
        ):
            continue  # installed and then taken away in the same command
        return True
    return False


def _subprocess_launches(module_path: str) -> tuple[list[ast.Call], list[ast.Call]]:
    """Split a module's subprocess launches into (literal git, unreadable).

    Narrowed to subprocess on purpose: an unrelated `record_requirement(["git"])`
    satisfied an earlier any-call version. The second list is what closes the hole
    a bare count could not: a launch whose command is built in a variable is
    invisible to this checker, so it is reported rather than skipped. A sixth
    untrusted variable-based git launch therefore fails instead of hiding behind
    five recognised ones.
    """
    tree = ast.parse(_read(module_path))
    literal_git: list[ast.Call] = []
    unreadable: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        launcher = (
            isinstance(func, ast.Attribute)
            and func.attr in {"run", "Popen", "check_output", "call"}
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )
        if not launcher or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.List) or not first.elts:
            unreadable.append(node)
            continue
        head = first.elts[0]
        if isinstance(head, ast.Constant) and head.value == "git":
            literal_git.append(node)
    return literal_git, unreadable


def test_the_image_build_never_selects_a_stage_other_than_the_last():
    """`_installs_git` inspects the final stage, which only ships if nothing
    overrides it. A `target:` input would silently point the build elsewhere."""
    workflow = _read(".github/workflows/build-platform-images.yml")
    build_step = workflow[workflow.index("Build and push"):]
    assert not re.search(r"^\s+target:", build_step, flags=re.MULTILINE), (
        "the build now selects an explicit stage; _installs_git must follow it"
    )


def test_every_git_launch_declares_the_repository_safe():
    """Installing git is necessary and not sufficient.

    Tenant repos sit on EFS and can be owned by the access-point UID rather than
    the container UID, so git refuses them with "detected dubious ownership", and
    running as root does not bypass that check. The harness has always handled this
    (tenantRepoProvider.ts trustSharedRepo); the Python service did not, so the app
    and broker would still have answered 503 with git installed.
    """
    calls, unreadable = _subprocess_launches("server/customization_service.py")
    assert calls, (
        "premise: the service launches git through subprocess with a literal command "
        "list. If that changed, this contract needs rewriting, not skipping."
    )
    assert not unreadable, (
        "customization_service.py:"
        + ", ".join(str(node.lineno) for node in unreadable)
        + ": a subprocess command built in a variable is invisible to this check. "
        "Keep git launches literal, or extend the checker."
    )
    for call in calls:
        elements = call.args[0].elts
        trusted = [
            el for el in elements
            if isinstance(el, ast.Starred)
            and isinstance(el.value, ast.Call)
            and isinstance(el.value.func, ast.Name)
            and el.value.func.id == "_git_trust"
        ]
        assert trusted, (
            f"customization_service.py:{call.lineno}: this git launch does not pass "
            "_git_trust(...), so an EFS-owned repo will be refused as dubious"
        )
        # Which paths, not merely that some argument exists. `_git_trust()` with no
        # arguments emits no flags, and `_git_trust(Path.cwd())` emits a flag for the
        # wrong directory: both passed an earlier version of this assertion, and the
        # second recreates the exact 503 it is here to prevent.
        operands: set[str] = set()
        for element in elements:
            if element in trusted:
                continue  # the trust flags themselves are not operands
            operands.update(
                node.id for node in ast.walk(element) if isinstance(node, ast.Name)
            )
        for starred in trusted:
            args = starred.value.args
            assert args, (
                f"customization_service.py:{call.lineno}: _git_trust() was called with "
                "no paths, so it produces no safe.directory flags at all"
            )
            names = {n.id for a in args for n in ast.walk(a) if isinstance(n, ast.Name)}
            assert names & operands, (
                f"customization_service.py:{call.lineno}: _git_trust{tuple(sorted(names))} "
                f"trusts nothing this command touches {tuple(sorted(operands))}. "
                "Trusting the wrong path leaves the real repository refused."
            )


def test_every_image_whose_process_requires_git_installs_git():
    """A missing binary is invisible to a test suite that only reads Python.

    `customization_service` launches git (`rev-parse --verify refs/heads/main`,
    `show`, `worktree add`) and `python:3.12-slim` ships none, so every call raised
    FileNotFoundError. The app answered 503 `tenant_repository_unavailable` for a
    repository the harness had already provisioned correctly, and the BROKER could
    not execute a published tenant tool either, because
    `tool_loader.resolve_local_file` reaches `effective_catalog_dir` which calls
    `_git_blob` on every resolution.

    Scoped to REQUIRED git, deliberately: `solver_adapters/autofill.py` probes for
    git and treats OSError as "unknown revision", so canonical-worker legitimately
    ships without it.
    """
    assert _subprocess_launches("server/customization_service.py")[0], (
        "premise: customization_service launches git. If that changed, this test's "
        "reason to exist changed with it."
    )

    for path in (
        "deploy/Dockerfile.app",        # stages and publishes authored tools
        "deploy/Dockerfile.broker",     # EXECUTES them, via the effective catalog
        "deploy/Dockerfile.harness",    # owns the tenant repos
    ):
        assert _installs_git(path), (
            f"{path}: the shipped stage must install git, which its process requires"
        )


def test_app_and_harness_images_are_ready_but_keep_legacy_defaults():
    app = _read("deploy/Dockerfile.app")
    harness = _read("deploy/Dockerfile.harness")
    harness_serve = _read("harness/scripts/serve.ts")
    package = _read("harness/package.json")

    assert "COPY platform/requirements.txt" in app
    assert "-r /app/platform/requirements.txt" in app
    assert "COPY platform/" in app
    assert "LEAF_JOBS_STORE=legacy" in app
    assert "LEAF_SESSIONS_STORE=legacy" in app
    assert "LEAF_AGENT_STORE=legacy" in app
    assert "LEAF_GUEST_CAP_STORE=memory" in app
    assert "LEAF_DRAWING_STORE=legacy" in app
    assert "LEAF_UPLOAD_STORE=legacy" in app
    assert "LEAF_CALLBACK_REPLAY_STORE=legacy" in _read("deploy/Dockerfile.broker")
    assert "LEAF_JOBS_STORE=legacy" in _read("deploy/Dockerfile.broker")
    assert "LEAF_DRAWING_STORE=legacy" in _read("deploy/Dockerfile.broker")

    assert '"pg"' in package
    assert "RUN npm ci" in harness
    assert "COPY harness/src/" in harness
    assert "LEAF_HARNESS_SESSION_STORE=file" in harness
    assert "LEAF_HARNESS_AUTHORING_MODE=disabled" in harness
    assert "information_schema.columns" in harness_serve
    assert "FROM pg_constraint" in harness_serve
    assert "FROM pg_indexes" in harness_serve
    assert "harness_tenant_repo_leases" in harness_serve
    assert "USER 10002:10002" in harness


def test_harness_bootstraps_tls_before_using_debian_package_sources():
    harness = _read("deploy/Dockerfile.harness")

    trust_copy = (
        "COPY --from=trust-store /etc/ssl/certs/ca-certificates.crt "
        "/etc/ssl/certs/ca-certificates.crt"
    )
    rewrite_marker = "s|http://deb.debian.org|https://deb.debian.org|g"
    assert "FROM node:22-bookworm AS trust-store" in harness
    assert trust_copy in harness
    assert rewrite_marker in harness
    assert "s|http://security.debian.org|https://security.debian.org|g" in harness
    assert (
        harness.index(trust_copy)
        < harness.index(rewrite_marker)
        < harness.index("apt-get update")
    )
    assert "apt-get install -y --no-install-recommends git ca-certificates" in harness


def test_base_compose_uses_explicit_legacy_defaults_and_separate_connections():
    compose = _read("docker-compose.yml")
    app = _service(compose, "app")
    broker = _service(compose, "broker")
    harness = _service(compose, "harness")

    assert "LEAF_SESSIONS_STORE: ${LEAF_SESSIONS_STORE:-legacy}" in app
    assert "LEAF_JOBS_STORE: ${LEAF_JOBS_STORE:-legacy}" in app
    assert "LEAF_AGENT_STORE: ${LEAF_AGENT_STORE:-legacy}" in app
    assert "LEAF_GUEST_CAP_STORE: ${LEAF_GUEST_CAP_STORE:-memory}" in app
    assert "LEAF_DRAWING_STORE: ${LEAF_DRAWING_STORE:-legacy}" in app
    assert "LEAF_UPLOAD_STORE: ${LEAF_UPLOAD_STORE:-legacy}" in app
    assert "LEAF_GUEST_CAP_HMAC_SECRET: ${LEAF_GUEST_CAP_HMAC_SECRET:-}" in app
    assert "DATABASE_URL: ${DATABASE_URL:-}" in app

    assert "LEAF_BROKER_STORE: ${LEAF_BROKER_STORE:-legacy}" in broker
    assert "LEAF_CALLBACK_REPLAY_STORE: ${LEAF_CALLBACK_REPLAY_STORE:-legacy}" in broker
    assert "LEAF_JOBS_STORE: ${LEAF_JOBS_STORE:-legacy}" in broker
    assert "LEAF_DRAWING_STORE: ${LEAF_DRAWING_STORE:-legacy}" in broker
    assert "DATABASE_URL: ${LEAF_BROKER_DATABASE_URL:-}" in broker
    assert "LEAF_GRANTS_DIR" not in broker
    assert "LEAF_GRANT_FILE" not in broker

    assert (
        "LEAF_HARNESS_SESSION_STORE: "
        "${LEAF_HARNESS_SESSION_STORE:-file}"
    ) in harness
    assert "LEAF_HARNESS_DATABASE_URL: ${LEAF_HARNESS_DATABASE_URL:-}" in harness
    assert "LEAF_HARNESS_AUTHORING_MODE: ${LEAF_HARNESS_AUTHORING_MODE:-disabled}" in harness


def test_opt_in_overlay_runs_migration_before_services_without_startup_ddl():
    overlay = _read("docker-compose.canonical.yml")
    migrate = _service(overlay, "migrate")

    assert "db.apply_migration(); db.assert_schema_current()" in migrate
    for name in ("app", "broker", "harness"):
        service = _service(overlay, name)
        assert "migrate:" in service
        assert "condition: service_completed_successfully" in service

    assert "LEAF_BROKER_STORE: ${LEAF_BROKER_STORE:-legacy}" in _service(
        overlay, "broker"
    )
    assert "LEAF_CALLBACK_REPLAY_STORE: ${LEAF_CALLBACK_REPLAY_STORE:-legacy}" in _service(
        overlay, "broker"
    )
    assert "LEAF_JOBS_STORE: ${LEAF_JOBS_STORE:-legacy}" in _service(
        overlay, "broker"
    )
    assert "LEAF_DRAWING_STORE: ${LEAF_DRAWING_STORE:-legacy}" in _service(
        overlay, "broker"
    )
    assert "LEAF_HARNESS_SESSION_STORE: ${LEAF_HARNESS_SESSION_STORE:-file}" in _service(
        overlay, "harness"
    )
    assert "LEAF_HARNESS_AUTHORING_MODE: ${LEAF_HARNESS_AUTHORING_MODE:-disabled}" in _service(
        overlay, "harness"
    )
    assert "LEAF_SESSIONS_STORE: ${LEAF_SESSIONS_STORE:-legacy}" in _service(
        overlay, "app"
    )
    assert "LEAF_JOBS_STORE: ${LEAF_JOBS_STORE:-legacy}" in _service(
        overlay, "app"
    )
    assert "LEAF_AGENT_STORE: ${LEAF_AGENT_STORE:-legacy}" in _service(
        overlay, "app"
    )
    assert "LEAF_GUEST_CAP_STORE: ${LEAF_GUEST_CAP_STORE:-memory}" in _service(
        overlay, "app"
    )
    assert "LEAF_DRAWING_STORE: ${LEAF_DRAWING_STORE:-legacy}" in _service(
        overlay, "app"
    )
    assert "LEAF_UPLOAD_STORE: ${LEAF_UPLOAD_STORE:-legacy}" in _service(
        overlay, "app"
    )

    for path in (
        "deploy/Dockerfile.app",
        "deploy/Dockerfile.broker",
        "deploy/Dockerfile.harness",
    ):
        dockerfile = _read(path)
        assert "apply_migration" not in dockerfile
        assert "CREATE TABLE" not in dockerfile
