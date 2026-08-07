"""Static checks for staged PostgreSQL container wiring.

These checks do not need a Docker daemon or a live database. They protect the
legacy defaults and process trust boundaries that must hold before an operator
can run the separate migration and cutover stages.
"""
from pathlib import Path, PurePosixPath
import ast
import fnmatch
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
#     not a static test. That assertion now EXISTS: deploy/Dockerfile.app runs a
#     `RUN test -f && test -s` over every shipped script as its last
#     filesystem-touching instruction (NOT its last instruction -- the
#     per-commit ARG and the image metadata follow it), so a destruction of any
#     spelling fails the BUILD, and
#     `test_the_image_asserts_its_own_reconcilers_at_build_time` below pins it.
#     Do not re-attempt the static version: the runtime one already decides it.
#
# What REMAINS parses only COPY, and every way it can be wrong is LOUD: a COPY
# form it fails to understand empties the map, and an unmapped script then
# raises "no COPY ... was found" rather than passing quietly.
#
# A SECOND KNOWN GAP, raised in review and consciously accepted rather than
# half-closed: a destination reached through a SYMLINK created earlier in the
# build. `RUN ln -s /app/scripts /alias` followed by `COPY data/blob /alias/x.py`
# replaces a tracked script, and every check below compares paths lexically, so
# none of them sees it. Closing it statically means knowing which directories are
# symlinks at each line, which means interpreting RUN's shell -- the exact thing
# the deleted survival check tried and could not do correctly. Matching `ln -s`
# with a regex would catch that spelling and miss `ln --symbolic`, a symlink made
# inside a script, or one already present in the base image, which is the failure
# mode that made the survival check worse than nothing: its green manufactured
# confidence. Deliberately not attempted here.
#
# AND IT IS NOT COVERED BY THE RUNTIME ASSERTION ABOVE, which is the easy wrong
# conclusion to draw now that one exists. `test -f && test -s` decides that a
# shipped script is PRESENT and NON-EMPTY, so it closes the DESTRUCTION class
# completely. An overwrite leaves a file that is present and non-empty and simply
# holds the wrong bytes, so it passes that assertion untouched. Closing THIS
# class in the image needs an assertion on CONTENT -- a digest of each shipped
# script compared against the repository's -- which does not exist yet in any
# lane. Nothing currently decides it. Do not read the runtime guard as covering
# it; the two classes look alike and are stopped by different things.
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


def _exec_argv(argument: str) -> list[str] | None:
    """The argv of an exec-form instruction, or None when it is not one.

    None rather than an exception for every non-exec-form argument, because the
    caller compares against an expected argv: a shell-form instruction must
    simply fail to match, not blow up the test that is scanning past it.

    Docker accepts only a JSON array of strings here. Anything that parses to
    something else is not exec form to Docker either, so returning None for it
    keeps this in step with the builder rather than being lenient where the
    builder is not.
    """
    text = argument.strip()
    if not text.startswith("["):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    if not all(isinstance(item, str) for item in parsed):
        return None
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
            # A destination ABOVE the script's directory still reaches it, and
            # this guard REFUSES that shape rather than adjudicating it.
            #
            # Docker merges a directory copy recursively and auto-extracts a
            # local tar, so `COPY overlay/ /app/` and `ADD payload.tar /app/` can
            # both replace a tracked script while naming a destination that
            # equals neither it nor its parent.
            #
            # A previous attempt DID adjudicate this, by asking the repository
            # tree whether the source carried a colliding path. Review found four
            # defects in that one check within a single round: `--from=` sources
            # a tree that is not this repository at all; docker detects an
            # archive by its CONTENT, so a tar named `payload.bin` extracted
            # anyway; the suffix rule refused the repo's real
            # `web/vendor/node_modules-linux-x64.tar.gz` even under COPY, which
            # never extracts; and globs, `--exclude=`, and .dockerignore all made
            # a legitimate source look absent. Two false negatives and two false
            # positives, in twenty lines.
            #
            # That is the pattern this whole module was rebuilt around: every one
            # of the review findings against it has been in the Dockerfile parse,
            # never in the contract it protects. So this stops parsing and
            # refuses, exactly as it already does for multi-stage builds, the
            # JSON form, `--from=` out of scripts/, and an escape directive.
            # deploy/Dockerfile.app copies into no ancestor of /app/scripts
            # today, so this costs nothing; the day it needs to, the message says
            # what to write instead.
            assert not _contains(destination, target), (
                f"{keyword} writes into {destination}, which is ABOVE {target}. "
                f"Docker merges a directory copy recursively and extracts a "
                f"local archive, so content under {source} could replace that "
                f"script and the operator's documented command would run the "
                f"wrong file. This guard deliberately does not try to decide "
                f"whether it actually does -- four ways of getting that wrong "
                f"were found in one review round. Copy to a destination that is "
                f"not above a shipped script, or teach this guard."
            )
    return copies


def _contains(directory: str, path: str) -> bool:
    """Whether `path` sits anywhere beneath `directory`."""
    prefix = directory.rstrip("/") + "/"
    return path.startswith(prefix) and path != directory.rstrip("/")


# ---------------------------------------------------------------------------- #
# The SAME survival guard, for the three images `_copied_scripts` cannot read.
#
# `_copied_scripts` above is deliberately narrow in ways that all hold for
# deploy/Dockerfile.app and none of which hold for the other three images. That
# narrowness is exactly how those images came to ship operator-reachable scripts
# with no build-time guard at all:
#
#   * it folds every stage together and is only ever called behind
#     `_single_stage`, while deploy/Dockerfile.broker and
#     deploy/Dockerfile.harness are both TWO-stage builds;
#   * it selects on a `scripts/` PREFIX, while the broker ships
#     `harness/scripts/e2b-tool-exec.mjs` -- a scripts path that is not a
#     scripts prefix, so the prefix test skips it in silence;
#   * it reads single-FILE sources, while the harness copies the whole
#     `harness/scripts/` DIRECTORY.
#
# So this REUSES the parse rather than repeating it. `_copy_operands` already
# refuses the JSON form, a variable-built path, a relative destination and a
# bad arity, and `_image_path`/`_contains` already answer where a destination
# really lands; all of that is inherited here. What is added is only what those
# three shapes need: the final stage instead of all stages, a `scripts` path
# COMPONENT instead of a prefix, and the directory form resolved against the
# repository tree. Every way the addition can fail to understand a COPY is LOUD
# -- ADD, a rename, `--from=`, a multi-source copy of a scripts path, a
# subdirectory, and a dockerignored entry each RAISE rather than quietly
# shrinking the guarded set, because a guard that silently covers less than it
# appears to is the failure mode this whole module was rebuilt around.
# ---------------------------------------------------------------------------- #
_SCRIPTS_COMPONENT = "scripts"

# The base allowlist of what may follow a survival guard: instructions that
# cannot remove a file at BUILD time. USER and VOLUME are NOT here, for the
# reasons spelled out in test_the_image_asserts_its_own_reconcilers_at_build_time.
_CANNOT_REMOVE = frozenset({
    "ARG", "CMD", "ENTRYPOINT", "ENV", "EXPOSE", "HEALTHCHECK", "LABEL",
    "MAINTAINER", "SHELL", "STOPSIGNAL", "WORKDIR",
})

_DOCKERIGNORE_BASENAME = re.compile(r"^\*\*/(?!.*/)(.+)$")


def _dockerignore_subset() -> tuple[set[str], set[str]]:
    """(basename globs, exact repo-relative paths) from the root .dockerignore.

    A deliberate SUBSET of docker's exclusion semantics, used only to fail LOUD
    and never to decide that something IS shipped. It exists because the
    directory form below enumerates the repository while the image holds only
    what the build context delivered: a `harness/scripts/debug.log` is excluded
    by `**/*.log`, so a guard derived from the directory would name a path that
    never ships and break every build. Catching it here turns a fail-closed
    build break into a test failure that says what to do.
    """
    basenames: set[str] = set()
    exact: set[str] = set()
    for raw in _read(".dockerignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # A negation re-includes a path and which rule wins depends on order.
        # This subset does not model that, so refuse rather than guess.
        assert not line.startswith("!"), (
            ".dockerignore now uses a negation rule, whose effect depends on "
            "rule order. Teach this subset before relying on it."
        )
        match = _DOCKERIGNORE_BASENAME.match(line)
        if match:
            basenames.add(match.group(1))
        else:
            exact.add(line.rstrip("/"))
    return basenames, exact


def _is_dockerignored(relative: str) -> bool:
    basenames, exact = _dockerignore_subset()
    name = PurePosixPath(relative).name
    if any(fnmatch.fnmatch(name, pattern) for pattern in basenames):
        return True
    return relative.rstrip("/") in exact


def _shipped_instructions(path: str) -> list[tuple[str, str]]:
    """The FINAL build stage's instructions -- the only ones that ship.

    Each `FROM` starts a fresh stage and a later stage inherits nothing it does
    not copy, so an earlier stage's COPY says nothing about the published image.
    `_single_stage` refuses a multi-stage file rather than reason about this,
    which is right for deploy/Dockerfile.app and unusable for the two images
    that ARE multi-stage. Reading the last stage is sound only because the build
    never selects another one, which
    `test_the_image_build_never_selects_a_stage_other_than_the_last` pins
    independently of anything here.
    """
    instructions = _instructions(_read(path))
    starts = [index for index, (keyword, _) in enumerate(instructions)
              if keyword == "FROM"]
    assert starts, f"{path} declares no FROM"
    return instructions[starts[-1]:]


_GLOB_METACHARACTERS = re.compile(r"[*?\[]")


def _writes_only_named_files(keyword: str, flags: list[str], sources: list[str]) -> bool:
    """Whether this instruction provably creates ONLY `destination/<basename>`.

    The ONE exception to the refuse-a-destination-above-a-script rule, and it is
    deliberately the narrowest shape that is decidable WITHOUT re-opening any of
    the four defects that got the adjudicating version deleted (recorded in
    `_copied_scripts`). Each of those defects is excluded by construction here
    rather than by a heuristic:

      * `ADD` auto-extracts a local archive detected by CONTENT, and a suffix
        rule for that was one of the four. So this accepts COPY only, which
        NEVER extracts -- no content inspection, no suffix guess.
      * `--from=` sources a tree that is not this repository, so nothing here
        could check it. Excluded outright, exactly as `_copied_scripts` does.
      * A glob's match set is not decidable here (`--exclude=` and .dockerignore
        both narrow it), and a glob can match a DIRECTORY. Any metacharacter is
        refused rather than expanded.
      * A source that is absent, or is a directory, is refused. Absent is the
        case a glob/ignore rule made "look missing", and it fails CLOSED here:
        unknown means refuse, never accept.

    What is left is a list of literal, existing, regular files. Docker writes
    exactly one entry per source into the destination directory, and creates no
    subdirectory, so such a line cannot reach anything under
    `destination/scripts/`. That is a statement about docker's file-copy
    semantics, not an inference about the source tree's contents.
    """
    if keyword != "COPY":
        return False
    if any(flag.startswith("--from=") for flag in flags):
        return False
    for source in sources:
        if _GLOB_METACHARACTERS.search(source):
            return False
        if not (REPO_ROOT / source).is_file():
            return False
    return True


def _shipped_scripts(path: str) -> dict[str, str]:
    """repository source path -> absolute in-image path, for the final stage.

    Keyed on the repository SOURCE for the same reason `_copied_scripts` is: a
    destination-keyed map asks only "does something land at this name", never
    "is it the right file", so a one-token slip ships one script's content under
    another's name and passes.
    """
    instructions = _shipped_instructions(path)
    shipped: dict[str, str] = {}
    destinations: list[tuple[str, str]] = []

    for keyword, argument in instructions:
        if keyword not in ("COPY", "ADD"):
            continue
        flags, sources, destination, is_directory = _copy_operands(keyword, argument)
        destinations.append((keyword, flags, sources, destination))

        scripted = [
            source for source in sources
            if _SCRIPTS_COMPONENT in PurePosixPath(source).parts
        ]
        if not scripted:
            continue

        # ADD can fetch a URL and auto-extracts an archive by its CONTENT, so
        # what it writes is not decidable from the operands.
        assert keyword == "COPY", (
            f"{path}: {keyword} ships {scripted}; ADD may fetch a remote source "
            "or unpack an archive, so this guard cannot say what it writes. "
            "Use COPY, or teach this guard."
        )
        assert not any(flag.startswith("--from=") for flag in flags), (
            f"{path}: COPY of {scripted} uses --from=, so it does not come from "
            "the repository tree; teach this guard before using that form."
        )
        assert len(sources) == 1, (
            f"{path}: COPY ships {scripted} alongside {sources}; with several "
            "sources each one's in-image path depends on the destination being "
            "a directory. Split it, or teach this guard."
        )

        source = sources[0]
        repo_path = REPO_ROOT / source
        if repo_path.is_dir():
            # DIRECTORY form: docker copies the CONTENTS into the destination,
            # so the repository directory IS the shipped set. Derived rather
            # than curated, which is what gives a directory copy the same
            # "covered the day it is copied" property a file copy gets.
            assert PurePosixPath(source.rstrip("/")).name == _SCRIPTS_COMPONENT, (
                f"{path}: COPY {source} has a `scripts` path component but is "
                "not itself a scripts directory; teach this guard."
            )
            assert is_directory, (
                f"{path}: COPY {source} {destination} copies a directory's "
                "CONTENTS, but the destination is not written as a directory. "
                "Give it a trailing slash."
            )
            entries = sorted(repo_path.iterdir(), key=lambda entry: entry.name)
            assert entries, f"{path}: COPY {source} ships an empty directory"
            for entry in entries:
                relative = f"{source.rstrip('/')}/{entry.name}"
                # Docker recurses, but the flat guard below would name a
                # directory as a file and fail every build.
                assert entry.is_file(), (
                    f"{path}: {relative} is not a plain file; the directory "
                    "COPY recurses but this guard only names flat entries."
                )
                assert not _is_dockerignored(relative), (
                    f"{path}: {relative} is excluded by .dockerignore, so the "
                    "image will not hold it and a guard naming it would fail "
                    "every build. Remove the file or narrow the ignore rule."
                )
                assert relative not in shipped, f"{relative} ships twice"
                shipped[relative] = posixpath.join(destination, entry.name)
            continue

        assert repo_path.is_file(), (
            f"{path}: COPY {source} names no file in the repository"
        )
        source_name = PurePosixPath(source).name
        # A directory DESTINATION for a single file resolves to dest/name, which
        # is an ordinary correct line, not a rename.
        landing = (posixpath.join(destination, source_name) if is_directory
                   else destination)
        assert PurePosixPath(landing).name == source_name, (
            f"{path}: COPY ships {source} as {landing}, renaming it. This guard "
            "identifies a shipped script by its repository name, so a rename "
            "means the image holds one script's content under another's name. "
            "Teach this guard before using that form."
        )
        assert not _is_dockerignored(source), (
            f"{path}: {source} is excluded by .dockerignore, so the COPY would "
            "fail and this guard would name a path the image never holds."
        )
        assert source not in shipped, f"{source} is COPYed to more than one target"
        shipped[source] = landing

    # A COPY need not come FROM a scripts path to land ON one. Checked on
    # DESTINATIONS, which need no WORKDIR reasoning, and using the same
    # equal/above pair `_copied_scripts` uses: docker merges a directory copy
    # recursively and extracts a local archive, so a destination ABOVE a shipped
    # script reaches it too. Refused rather than adjudicated, for the four
    # reasons recorded there.
    targets = set(shipped.values())
    parents = {posixpath.dirname(target) for target in targets}
    for keyword, flags, sources, destination in destinations:
        outsiders = [
            source for source in sources
            if _SCRIPTS_COMPONENT not in PurePosixPath(source).parts
        ]
        if not outsiders:
            continue

        # Landing exactly ON a shipped script, or on its directory. Unconditional:
        # even a copy that writes only the files it names writes them INTO a
        # destination directory, so `COPY data/blob /app/scripts/` still collides.
        assert destination not in targets | parents, (
            f"{path}: {keyword} writes {outsiders} to {destination}, which lands "
            "on a guarded script path, so the image would ship that content "
            "under a shipped script's name. Teach this guard before overwriting "
            "one."
        )

        # Landing somewhere ABOVE a shipped script, which reaches it too.
        # `_copied_scripts` refuses this shape outright rather than adjudicate
        # it, for four reasons recorded there, and deploy/Dockerfile.app never
        # needed the exception. deploy/Dockerfile.harness does: it copies
        # `harness/package.json harness/package-lock.json` into /app/, which is
        # strictly above /app/scripts. That line cannot reach the scripts
        # directory, and saying so does not require re-opening any of the four
        # defects -- see `_writes_only_named_files`.
        if _writes_only_named_files(keyword, flags, sources):
            continue
        for target in targets:
            assert not _contains(destination, target), (
                f"{path}: {keyword} writes into {destination}, which is ABOVE "
                f"{target}. Docker merges a directory copy recursively and "
                f"extracts a local archive, so content under {outsiders} could "
                "replace a shipped script. This guard deliberately does not try "
                "to decide whether it actually does. Copy somewhere that is not "
                "above a shipped script, or teach this guard."
            )

    # Anti-vacuity for the file as a whole: an image this guard covers that
    # copies no script means either the script stopped shipping or the parse
    # stopped seeing it, and both are blockers.
    assert shipped, f"{path} ships no script this guard can see"
    return shipped


def _survival_guard_argv(
    targets, *, readable: bool = False, executable: str | None = None,
    consume_arg: str | None = None,
) -> list[str]:
    """The one exec-form argv every image's survival guard must be spelled as.

    ONE definition for all four images, so a change to HOW the guard is spelled
    is a single edit that reddens all of them rather than four edits that drift
    apart. `-s` as well as `-f`, because `-f` alone accepts an EMPTY file, so a
    truncation ships a script that exits 0 and emits nothing. Sorted, so the
    expected string does not depend on the order the COPYs happen to appear in.

    `readable` and `executable` are what make the guard mean something on an
    image that DROPS PRIVILEGE. The clauses are identical whatever uid runs
    them; what changes is WHO runs them, which is why the guard is placed below
    a `USER` rather than given a cleverer command. See
    `_assert_one_survival_guard` for the identity rule that pairs with this.

    `executable` is DERIVED, not curated: it is the image's exec-form CMD's
    argv[0] when that is itself a guarded script. deploy/Dockerfile.harness's
    CMD *is* /app/scripts/start-harness.sh, so a mode that leaves it readable
    but not executable ships a container that cannot start; broker and
    canonical-worker invoke an interpreter instead, so they need no `-x`.
    """
    clauses = []
    for target in sorted(targets):
        clause = f"test -f {target} && test -s {target}"
        if readable:
            clause += f" && test -r {target}"
        clauses.append(clause)
    command = " && ".join(clauses)
    if executable:
        command += f" && test -x {executable}"
    # `consume_arg` appends a reference to a per-commit ARG so the guard may sit
    # BELOW that ARG without tripping the build-workflow cache contract (every
    # RUN below a per-commit ARG must reference one). Only canonical-worker needs
    # it: its per-commit ARGs are forced high in the file, so its guard -- which
    # must run LAST, after the root RUNs those ARGs feed -- is necessarily below
    # them. `test -n`, so an empty value also fails the build.
    if consume_arg:
        command += f' && test -n "${{{consume_arg}}}"'
    return ["/bin/sh", "-c", command]


def _user_in_effect(instructions: list[tuple[str, str]], upto: int | None = None) -> str:
    """The uid a RUN at `upto` executes as, or the image's runtime uid if None.

    Docker's USER is stateful: it applies to every later RUN in the stage AND to
    the container. "root" stands for the default when no USER has been declared.
    """
    current = "root"
    for keyword, argument in instructions[:upto]:
        if keyword == "USER":
            current = argument.strip()
    return current


def _cmd_executable(instructions: list[tuple[str, str]], targets) -> str | None:
    """The image's exec-form CMD argv[0], when it is itself a guarded script."""
    for keyword, argument in reversed(instructions):
        if keyword != "CMD":
            continue
        argv = _exec_argv(argument)
        # A shell-form CMD runs through /bin/sh, so argv[0] is the shell and
        # this guard cannot say which script it ends up executing.
        assert argv, (
            f"CMD {argument!r} is not exec form, so this guard cannot tell "
            "which file the container executes. Use exec form, or teach it."
        )
        return argv[0] if argv[0] in set(targets) else None
    return None


def _assert_one_survival_guard(
    path: str,
    instructions: list[tuple[str, str]],
    targets,
    allowed_after: frozenset,
    allowed_runs_after: tuple = (),
    *,
    readable: bool = False,
    executable: str | None = None,
    consume_arg: str | None = None,
) -> int:
    """Exactly one exec-form guard over `targets`, and nothing unchecked below it.

    EXEC FORM is load-bearing rather than a style choice: shell-form RUN
    executes through whatever SHELL is in effect, so a `SHELL ["/bin/true"]`
    placed ABOVE a byte-identical shell-form guard runs it as
    `/bin/true -c "test -f ..."` -- exit 0, nothing tested, image ships with the
    scripts deleted. That attack sits ABOVE the guard, where no rule about what
    FOLLOWS it could ever see it. Exec form names its own interpreter and
    ignores SHELL, which closes it outright instead of adding a position rule.
    """
    expected = _survival_guard_argv(
        targets, readable=readable, executable=executable, consume_arg=consume_arg)
    guarded = [
        index for index, (keyword, argument) in enumerate(instructions)
        if keyword == "RUN" and _exec_argv(argument) == expected
    ]
    assert len(guarded) == 1, (
        f"{path} must hold exactly one survival guard, in EXEC form, spelled "
        f"exactly:\n    RUN {json.dumps(expected)}\nfound {len(guarded)}. A "
        "guard the image ships must assert every script the COPY map ships, "
        "must name its own interpreter so a SHELL above it cannot neuter it, "
        "and nothing else may impersonate it."
    )

    # ALLOWLIST, not a denylist: an instruction this guard does not recognise
    # fails LOUD rather than being assumed harmless.
    offending = sorted({
        keyword for keyword, argument in instructions[guarded[0] + 1:]
        if keyword.upper() not in allowed_after
        and not (keyword == "RUN" and argument in allowed_runs_after)
    })
    assert not offending, (
        f"{path} runs {offending} AFTER the survival guard, so those "
        "instructions ship unchecked. Put them above the guard, or teach this "
        "allowlist why they cannot remove a file at BUILD time and cannot mask "
        "or re-permission the path for the runtime process."
    )
    return guarded[0]


def _assert_guard_runs_as_the_runtime_identity(
    path: str, instructions: list[tuple[str, str]], guard_index: int
) -> str:
    """On an image that drops privilege, the guard must run AS the runtime uid.

    THIS REPLACED TWO CHECKS THAT BOTH FAILED, and the history is the argument
    for why it is shaped this way.

    Round 1 tried to EVALUATE modes: it matched one `chmod` spelling and checked
    one bit (world read). Six evasions were found in one round -- `chmod 0744`
    on the directory (loses o+x traversal), `chmod 0444` on the harness CMD
    (loses +x), `install -m 0600`, `COPY --chmod=0600`, a quoted operand, and a
    multi-target chmod.

    Round 2 tried to REFUSE instead: any mode-changing command had to be
    byte-exact pinned. That fell to `install --mode=0600` (GNU's long form,
    which the `-m` pattern never saw) and to `c''hmod`, which is the general
    defeat -- shell token construction means no scan of command TEXT can
    enumerate the ways to spell a command.

    Both rounds were the same mistake in different clothes: paying for a `USER`
    with a text scan. The answer is not a better scan. The guard already runs a
    real filesystem test at build time, so the fix is to run it AS the identity
    that matters, and Docker's own `USER` does that with no `su`, no `setpriv`,
    and no dependency this repo cannot verify.

    So the rule is structural: the uid in effect where the guard runs must equal
    the image's runtime uid. Then `test -r` and `test -x` in the guard are
    evaluated against the real uid, gid and directory traversal, and NO spelling
    of a mode change above the guard can pass -- because nothing about the
    spelling is being read. A mode change BELOW the guard is separately refused
    by the instruction allowlist.

    Returns the runtime identity so the caller can build the expected argv.
    """
    runtime = _user_in_effect(instructions)
    at_guard = _user_in_effect(instructions, guard_index)
    assert at_guard == runtime, (
        f"{path}: the survival guard runs as {at_guard!r} but the container "
        f"runs as {runtime!r}. A guard that runs as root proves nothing about "
        f"what {runtime!r} can read or execute: root reads a 0600 file happily, "
        "and a directory chmod that removes o+x makes the script unreachable "
        "while every `test -f` still passes. Declare the runtime USER above the "
        "guard. Two attempts to pay for this by scanning command text were "
        "defeated -- by `install --mode=`, and by `c''hmod` -- so do not "
        "reintroduce one."
    )
    return runtime




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

    # A destination ABOVE the script's directory reaches it too: docker merges a
    # directory copy recursively and auto-extracts a local tar, so neither
    # equals the target or its parent while both replace the target. REFUSED as
    # a shape, without asking what the source contains -- deciding that was
    # tried and produced two false negatives and two false positives in one
    # review round. Every form below is refused by the same single assertion,
    # which is the point: none of them needs its own reasoning.
    for above in (
        "ADD data/payload.tar /app/\n",          # auto-extracted by content
        "ADD data/payload.bin /app/\n",          # a tar that is not named one
        "COPY overlay/ /app/\n",                 # not in the repository tree
        "COPY docs/ /app/\n",                    # in the tree, no collision
        "COPY . /app/\n",                        # the whole build context
        "COPY docs/*.md /app/\n",                # a glob
        "COPY --from=other docs/ /app/\n",       # a tree that is not this repo
        "COPY --exclude=scripts/x.py . /app/\n",  # an exclusion this cannot read
        "ADD https://example.com/a.tar /app/\n",  # remote, not unpacked
    ):
        with pytest.raises(AssertionError, match="which is ABOVE"):
            _copied_scripts(shipped + above)

    # A SIBLING is not an ancestor and must stay allowed, or this refusal would
    # swallow deploy/Dockerfile.app's own `COPY server/ /app/server/`.
    assert _copied_scripts(shipped + "COPY docs/ /app/docs/\n") == {
        "x.py": "/app/scripts/x.py"
    }
    assert _copied_scripts(shipped + "COPY server/ /app/server/\n") == {
        "x.py": "/app/scripts/x.py"
    }
    # An archive copied to a sibling is ordinary and correct: COPY never
    # extracts, and this is a real path in this repository.
    assert _copied_scripts(
        shipped + "COPY web/vendor/node_modules-linux-x64.tar.gz /app/web/\n"
    ) == {"x.py": "/app/scripts/x.py"}

    # `_contains` decides WHICH copies get the ancestor treatment, so pin its
    # boundary directly. A prefix is not a parent: /app/script must not be read
    # as containing /app/scripts/x.py, and a directory does not contain itself.
    assert _contains("/app", "/app/scripts/x.py")
    assert _contains("/app/scripts", "/app/scripts/x.py")
    assert _contains("/app/", "/app/scripts/x.py")
    assert not _contains("/app/script", "/app/scripts/x.py")
    assert not _contains("/app/docs", "/app/scripts/x.py")
    assert not _contains("/app/scripts/x.py", "/app/scripts/x.py")

    # And pin it through the real path too: a SIBLING copy whose source would
    # collide only if the boundary were lost must stay allowed. Without the
    # boundary, `/app/docs` reads as an ancestor, the relative path becomes
    # ../scripts/<name>, and that resolves to a file that really does exist.
    assert _copied_scripts(
        "COPY scripts/reconcile_customization_authority.py "
        "/app/scripts/reconcile_customization_authority.py\n"
        "COPY docs/ /app/docs/\n"
    ) == {
        "reconcile_customization_authority.py":
            "/app/scripts/reconcile_customization_authority.py"
    }

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

    WHAT A DAEMON HAS ACTUALLY RUN. This test decides resolution by PARSING the
    Dockerfile and the documented sources; a daemon has now EXECUTED the
    documented commands in the BUILT release image, which is the gap #509 left
    open. #509 demonstrated the customization command against a filesystem
    layout REPLAYED from this Dockerfile's COPY lines and final WORKDIR, not the
    release image, and never touched PostgreSQL. On tree
    465cbd5fddc1d0d290eef1e13f4a4b172d57e359 (the tree of main, `git rev-parse
    HEAD^{tree}`, not a commit id), a daemon built `leaf-app:reconciler-proof`
    from deploy/Dockerfile.app -- `docker image inspect` reported WorkingDir
    /app/server -- and ran each documented command with `--entrypoint sh` so the
    WORKDIR was left untouched and the echoed cwd is the command's own, against a
    live postgres:16 migrated by the image's /app/platform/db module:

      * Both customization commands (backfill, parity) ran from cwd /app/server
        and emitted `leaf.customization-authority-reconciliation.v2` receipts;
        `reconcile_sessions_authority.py --mode backfill` emitted
        `leaf.sessions-authority-reconciliation.v2`. The absolute /app/scripts
        path resolved and the script ran, in the only environment it is ever run.
      * The CONTRAST held on a real case-sensitive image: the repo-relative
        `python scripts/<name>` and the miscased `/app/scripts/Reconcile_*` each
        exited non-zero with "No such file or directory". The miscase only a
        Linux image decides; the Windows host resolves it, so only a daemon here
        can prove it.
      * `reconcile_sessions_authority.py --mode parity` RESOLVED and ran from
        /app/server, then declined the bare image rather than fabricate a
        result: it reads SESSIONS_DB (default /app/server/sessions.db), which
        deploy/Dockerfile.app deliberately ships none of, so it exited 2 with no
        receipt. That is the documented posture, not a defect -- authority-
        inventory.json app_sessions parity records "a cutover receipt cannot be
        manufactured out of a source that is simply gone" and status
        implemented_not_run. A meaningful sessions parity needs production's
        durable SESSIONS_DB=/data/state/sessions.db and stays unrun here.

    The receipt came from a daemon harness (prove_reconcilers_in_image.py, not
    committed). Once it is gone nothing here re-proves it; the static assertions
    below are what stand.
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


def test_the_image_asserts_its_own_reconcilers_at_build_time():
    """The destruction class this repo could not close statically.

    An instruction that removes a copied script leaves every check above green:
    `_copied_scripts` still reads the COPY line, and the documented command
    still resolves to its target. The post-COPY survival check that tried to
    catch it is gone (module comment above) because deciding it needs the
    WORKDIR in effect at each line, so a relative `RUN rm -rf scripts` in the
    region where WORKDIR is still /app read as harmless.

    The image decides it instead. A final `test -f` per shipped script runs
    AFTER every instruction above it, knows nothing about spelling, and fails
    the BUILD rather than a test.

    WHAT A DAEMON HAS ACTUALLY RUN, split by which SPELLING of the guard it ran,
    because the two are not interchangeable and only one of them ships.

      * SHELL form: mutation-proven locally with `docker build -f
        deploy/Dockerfile.app .`. Unmodified green. Dropping the sessions COPY,
        inserting `RUN rm -rf scripts` under WORKDIR /app, and truncating a
        script with `: > ...` each turned it red at this exact step.
      * EXEC form, the spelling that ships: its PASS path only, once, in CI on
        the PR that introduced it. BuildKit reported `#31 DONE 0.1s`, building
        tree `27bdb1399ab256d849e1821710fbdee8d7b3448a` as
        `leaf-platform-app:spec-27bdb1399ab256d849e1821710fbdee8d7b3448a-4aaf69f65ecc`,
        Actions run 31157638863. Git still confirms this is the TREE OF the
        commit which introduced exec form -- they are different objects, so
        `git rev-parse <commit>^{tree}` is the check, not `git cat-file -t` on
        the tree. Git does NOT confirm that BuildKit ran it, and once the
        Actions log expires nothing here does.

    Read the log for `DONE`, not for a green job. A later build reported
    `#31 CACHED` at this vertex, which proves only that some earlier build of an
    identical layer succeeded, never that this one ran the guard.

    NOT daemon-proven, and NOT closable by this test: every FAIL path of the exec
    form. Deletion, truncation, and the `SHELL ["/bin/true"]` neutering that
    motivated exec form have been replayed against the shell spelling or against
    the static assertions here, never against exec form on a daemon. Do not read
    the static coverage as standing in for them -- this test pins the guard's
    argv, and no assertion about a Dockerfile's text can observe a file that is
    missing or empty at build time. What the exec-form fail paths rest on is that
    `test -f` and `test -s` do not change meaning with the interpreter, plus
    Docker's documented rule that SHELL does not affect the exec form. Replaying
    all three against a daemon is what retires this paragraph.

    Keyed on the COPY map, so a reconciler added later is covered the day it is
    copied rather than the day someone remembers to extend this list.

    It is NOT the last instruction in the file, and must not be: a RUN below the
    per-commit `ARG LEAF_SOURCE_SHA` re-executes on every merge, which
    scripts/test_build_platform_images_workflow.py forbids outright. That was
    caught by CI, not by review. THREE rules cover the positions between them,
    and it takes all three -- a destructive instruction above the guard is
    caught by the guard, a RUN below the ARG by that invariant, and anything
    else below the guard by the allowlist here. A COPY or ONBUILD below the ARG
    escapes the first two; only the allowlist stops it.

    SCOPE, so this does not read as more than it is. It covers the app image's
    reconcilers, and only those. The other three images carry the same guard in
    the same spelling now, pinned by
    test_every_image_asserts_its_shipped_scripts_at_build_time -- but on a
    WEAKER footing, because no daemon has built them; that test says so in its
    own words and must not inherit this one's build-proof language. The argv and
    the exactly-one/allowlist rules are shared with it through
    `_survival_guard_argv` and `_assert_one_survival_guard`, so the spelling
    cannot drift between the four images. What differs per image is only the
    COPY map it is derived from and the allowlist it needs, both stated there.
    """
    dockerfile = _app_dockerfile()
    _single_stage(dockerfile)

    copies = _copied_scripts(dockerfile)
    assert copies, "deploy/Dockerfile.app copies no scripts/ file"

    # THE EXACT COMMAND, not a substring of it. Matching `"test -f" in argument`
    # was the first version and it was wrong in the same way the git-install
    # scan above was wrong. A review found the proof:
    #     RUN rm -rf scripts
    #     RUN echo test -f /app/scripts/reconcile_customization_authority.py \
    #       && echo test -f /app/scripts/reconcile_sessions_authority.py
    # Both instructions sit above the ARG, the second contains "test -f" and
    # names every copied target, nothing writable follows it, and the ARG
    # invariant sees no offender -- so the image shipped with both reconcilers
    # DELETED and every static gate green. Confirmed by running it. Pinning the
    # exact form makes that impossible rather than merely unlikely, which is the
    # same trade `_PINNED_GIT_INSTALL` makes: changing HOW the guard is spelled
    # now requires editing this pin, and that is a change a reviewer should see.
    # EXEC FORM, pinned as such. Pinning the tokens was not enough on its own:
    # shell-form RUN executes through whatever SHELL is in effect, so
    #     RUN rm -rf /app/scripts
    #     SHELL ["/bin/true"]
    # ran the byte-identical guard as `/bin/true -c "test -f ..."`, which exits
    # 0 having tested nothing. The image shipped with both reconcilers deleted
    # and BOTH this test and test_documented_authority_commands_resolve_in_the_image
    # passed. A review confirmed it by replaying the mutation. That attack sits
    # ABOVE the guard, where the allowlist below cannot see it by construction,
    # so no rule about what FOLLOWS the guard could ever have caught it. Naming
    # the interpreter closes it outright instead of adding another position
    # rule: exec form ignores SHELL, so nothing above the guard changes what
    # the guard means.
    # THE EXACT COMMAND and the exactly-one/allowlist rules live in
    # `_survival_guard_argv` and `_assert_one_survival_guard`, shared with the
    # other three images. The reasoning above is the reasoning they encode; it
    # is stated here because this is where its history is.
    #
    # deploy/Dockerfile.app takes the BASE allowlist with no additions. USER and
    # VOLUME are deliberately absent, though neither deletes a file: the guard
    # runs as root at build time, so a later `USER 10001` can leave a 0600
    # script unreadable to the process that actually runs it, and a later
    # `VOLUME /app/scripts` masks the directory at runtime. Both ship an image
    # whose guard passed and whose documented command still fails. This image
    # declares neither, so it needs no exception. The three images that DO drop
    # privilege declare their runtime USER ABOVE the guard, so the guard runs as
    # that uid and `_assert_guard_runs_as_the_runtime_identity` enforces the
    # pairing; `test -r`/`test -x` then observe what the runtime uid can actually
    # reach at BUILD time rather than merely bounding it (no command text is
    # read, so the evasions that beat two earlier mode scanners do not apply).
    # What a later CMD/HEALTHCHECK can do at RUNTIME remains a review class,
    # recorded in test_every_image_asserts_its_shipped_scripts_at_build_time.
    #
    # WHAT THIS CANNOT DECIDE, stated so the allowlist is not read as more than
    # it is: the members that carry a payload -- CMD, ENTRYPOINT, HEALTHCHECK --
    # run arbitrary commands at RUNTIME, and ENV can point PATH or PYTHONPATH
    # somewhere else. A `HEALTHCHECK CMD rm -rf /app/scripts/...` below the
    # guard passes this test, passes the build, and deletes the script seconds
    # into container life while the container reports healthy. No rule about
    # POSITION can bind runtime behaviour, so that is a review class, not a gate.
    _assert_one_survival_guard(
        "deploy/Dockerfile.app",
        _instructions(dockerfile),
        copies.values(),
        _CANNOT_REMOVE,
    )


# image -> (the operator-reachable script that motivated its guard,
#           extra instruction keywords its allowlist must admit,
#           exact RUN bodies its allowlist must admit,
#           the runtime uid the guard must run as,
#           the per-commit ARG the guard must consume, or None)
#
# All three images declare their runtime USER ABOVE their guard, so the guard's
# `test -r` (and `test -x` on the harness CMD) are evaluated as the uid the
# container runs as; `_assert_guard_runs_as_the_runtime_identity` enforces that
# pairing. broker and harness admit USER after the guard defensively; each
# already has its guard as the last filesystem-touching instruction and needs no
# pinned RUN after it.
#
# canonical-worker is the one image whose guard consumes a per-commit ARG, and
# the reason is #514 round 3. Its per-commit ARGs are forced high in the file (a
# layer-cache contract), and two RUNs below them run as ROOT -- one imports
# mutable Python (solver_adapters.autofill.attest_source). Round 3 sat the guard
# ABOVE those RUNs and byte-pinned them, but a byte pin binds the Dockerfile
# text, not the code that RUN imports: an edit to attest_source could `chmod`
# /app/scripts as root after the guard passed, and the image shipped with uid
# 65532 locked out while every static check stayed green (sol-critic). The fix
# is the module's own thesis applied once more -- do not scan or pin text, run
# the real filesystem test as the real identity: the guard now runs LAST, as uid
# 65532, AFTER the root window, observing whatever those RUNs left on disk.
# Sitting below the ARG it must reference one, so it consumes LEAF_SOURCE_SHA;
# its allowlist admits NOTHING after it (empty extra, no pinned RUNs), which is
# exactly what forces it past the two root RUNs.
_GUARDED_IMAGES = {
    "canonical-worker": (
        "/app/scripts/canonical-container-smoke.py",
        frozenset(),
        (),
        "65532:65532",
        "LEAF_SOURCE_SHA",
    ),
    "broker": ("/app/harness/scripts/e2b-tool-exec.mjs", frozenset({"USER"}), (),
               "10001:10001", None),
    "harness": ("/app/scripts/start-harness.sh", frozenset({"USER"}), (),
                "10002:10002", None),
}


# The two root RUNs canonical-worker runs BEFORE it drops to uid 65532 for the
# trailing survival guard. Round 3 pinned these byte-exact only as "allowed after
# the guard"; the round-4 guard move deleted that coupling, and sol-critic round
# 4 caught the consequence. With the guard no longer above them AND their text no
# longer pinned, DELETING the attestation RUN shipped an image that builds a
# mutable, UNATTESTED solver context as the claimed revision, while
# `assert "attest_source" in dockerfile` (test_canonical_worker.py) rode on a
# comment. Provenance is a SEPARATE contract from script survival, so it gets its
# own pin here: both RUNs must be present verbatim as parsed RUN instructions --
# a comment cannot satisfy this, and editing either is a change a reviewer sees.
# EXEC form (`["/bin/sh","-c",...]`), pinned verbatim. Round 5 shipped these as
# shell form and sol-critic caught the consequence: a `SHELL ["/bin/true"]`
# inserted above ran them as `/bin/true -c "..."` -- no attest, no seal -- while
# these byte-exact pins and every static gate stayed green. Exec form names
# /bin/sh directly and ignores SHELL, the same reason the survival guard is exec
# form. The pins are the parsed RUN ARGUMENT text; the test also requires exec
# form via _exec_argv, so a revert to shell form reddens.
_CANONICAL_WORKER_ATTESTATION_RUNS = (
    '["/bin/sh", "-c", "PYTHONPATH=/app/server python -c \\"from pathlib import Path; from solver_adapters.autofill import attest_source; attest_source(Path(\'/opt/leaf/autofill-solver\'), \'${AUTOFILL_SOLVER_REVISION}\', Path(\'/app/deploy/autofill-solver-sources.json\'))\\""]',
    '["/bin/sh", "-c", "python -c \\"import re,sys; value=sys.argv[1]; raise SystemExit(0 if re.fullmatch(r\'[0-9a-f]{40}\', value) else \'AUTOFILL_SOLVER_REVISION must be an exact lowercase 40-character commit\')\\" \\"$AUTOFILL_SOLVER_REVISION\\" && python -c \\"import re,sys; value=sys.argv[1]; raise SystemExit(0 if re.fullmatch(r\'[0-9a-f]{40}\', value) else \'LEAF_SOURCE_SHA must be an exact lowercase 40-character commit\')\\" \\"$LEAF_SOURCE_SHA\\" && printf \'%s\\\\n\' \\"$AUTOFILL_SOLVER_REVISION\\" > /opt/leaf/autofill-solver/.leaf-source-revision && printf \'%s\\\\n\' \\"$LEAF_SOURCE_SHA\\" > /app/.leaf-source-revision"]',
)


def test_canonical_worker_pins_its_solver_attestation_runs():
    """Provenance: canonical-worker must attest its solver source and seal the
    revision, and neither RUN may be deleted or edited unnoticed.

    DECOUPLED from the survival guard on purpose. The guard proves the smoke
    script survived the build as uid 65532; it says nothing about whether the
    solver source was attested. Round 3 conflated the two by pinning these RUNs
    as "allowed after the guard"; the round-4 move (guard last) dropped that
    coupling and with it the only real provenance enforcement -- so deleting the
    attestation RUN built an image with a mutable, unattested solver context
    labelled as the claimed revision, while the substring pin in
    test_canonical_worker.py rode on a comment. Byte-exact PARSED RUNs close it:
    a comment cannot satisfy this, and any edit reddens here.
    """
    run_args = [arg for kw, arg in
                _instructions(_read("deploy/Dockerfile.canonical-worker"))
                if kw == "RUN"]
    for pinned in _CANONICAL_WORKER_ATTESTATION_RUNS:
        assert pinned in run_args, (
            "deploy/Dockerfile.canonical-worker no longer runs this attestation/"
            f"revision-sealing RUN verbatim:\n    {pinned}\nparsed RUNs:\n    "
            + "\n    ".join(run_args)
        )
        # EXEC form is load-bearing, not style: a shell-form provenance RUN can be
        # neutered by a `SHELL ["/bin/true"]` above it (sol-critic #514 r5), and
        # the byte-exact pin above would not see it because the RUN text is
        # unchanged. Requiring exec form here closes that, and a revert to shell
        # form reddens both this and the pin above.
        assert _exec_argv(pinned) is not None, (
            "this provenance RUN must be EXEC form so a SHELL above it cannot turn "
            f"it into a no-op:\n    {pinned}"
        )


def test_the_widened_script_parse_refuses_every_form_it_cannot_read():
    """The three images' guards are only as good as this parse, so pin it.

    Each form below is one the narrow `_copied_scripts` drops in SILENCE, which
    is how three images came to ship unguarded scripts in the first place. A
    parse that quietly shrinks the guarded set produces a green test over an
    image that asserts nothing, so every gap here must RAISE.
    """
    # Only the FINAL stage ships: an earlier stage's COPY says nothing about the
    # published image, and crediting a guard for a file that never arrived is
    # exactly the silent-shrink failure. `_single_stage` refuses a multi-stage
    # file rather than reason about it, and TWO of the three guarded images
    # really are multi-stage -- which is the whole reason `_shipped_instructions`
    # had to exist. Pin that premise, so this does not quietly become a
    # single-stage problem that `_copied_scripts` could have handled all along.
    for image in ("broker", "harness"):
        with pytest.raises(AssertionError, match="stages"):
            _single_stage(_read(f"deploy/Dockerfile.{image}"))
    _single_stage(_read("deploy/Dockerfile.canonical-worker"))

    # ...and the last stage is the one read.
    two_stages = (
        "FROM node:20-slim AS deps\n"
        "COPY harness/scripts/ /app/scripts/\n"
        "FROM python:3.12-slim AS final\n"
        "COPY server/ /app/server/\n"
    )
    assert [k for k, _ in _instructions(two_stages)] == [
        "FROM", "COPY", "FROM", "COPY"]

    # The guard argv is order-independent and always carries BOTH tests.
    assert _survival_guard_argv(["/b", "/a"]) == [
        "/bin/sh", "-c", "test -f /a && test -s /a && test -f /b && test -s /b",
    ]

    # Exactly one guard, in exec form. A shell-form spelling of the IDENTICAL
    # command must not satisfy it: a `SHELL ["/bin/true"]` above it would run
    # the byte-identical guard as `/bin/true -c "test -f ..."`, exit 0, and test
    # nothing.
    argv = _survival_guard_argv(["/app/scripts/x.py"])
    exec_form = ("RUN", json.dumps(argv))
    _assert_one_survival_guard("probe", [exec_form], ["/app/scripts/x.py"],
                               _CANNOT_REMOVE)
    with pytest.raises(AssertionError, match="exactly one survival guard"):
        _assert_one_survival_guard("probe", [("RUN", argv[2])],
                                   ["/app/scripts/x.py"], _CANNOT_REMOVE)

    # An instruction the allowlist does not name fails LOUD; a named one passes.
    with pytest.raises(AssertionError, match=r"runs \['USER'\]"):
        _assert_one_survival_guard("probe", [exec_form, ("USER", "10001")],
                                   ["/app/scripts/x.py"], _CANNOT_REMOVE)
    _assert_one_survival_guard("probe", [exec_form, ("USER", "10001")],
                               ["/app/scripts/x.py"], _CANNOT_REMOVE | {"USER"})
    # A RUN below the guard passes ONLY when its exact body is pinned.
    with pytest.raises(AssertionError, match=r"runs \['RUN'\]"):
        _assert_one_survival_guard("probe", [exec_form, ("RUN", "rm -rf /app")],
                                   ["/app/scripts/x.py"], _CANNOT_REMOVE)
    _assert_one_survival_guard("probe", [exec_form, ("RUN", "seal $X")],
                               ["/app/scripts/x.py"], _CANNOT_REMOVE, ("seal $X",))

    # THE GUARD MUST RUN AS THE RUNTIME IDENTITY. This replaced two text scans
    # that were each defeated, and the point of the replacement is that it reads
    # no command text at all -- so the mutations that killed both predecessors
    # are closed by construction rather than by another pattern.
    #
    # `chmod 0744 /app/scripts` (directory loses o+x), `chmod 0444` on the CMD
    # (loses +x), `install -m 0600`, `install --mode=0600` (the GNU long form
    # that killed round 2), `COPY --chmod=0600`, a quoted operand, a multi-target
    # chmod, and `c''hmod` (shell token construction, the general defeat of any
    # text scan) are ALL caught now, because the guard executes `test -r`/`test
    # -x` as the real uid and no spelling changes what the filesystem reports.
    assert _user_in_effect([("USER", "10002:10002"), ("RUN", "x")]) == "10002:10002"
    assert _user_in_effect([("RUN", "x")]) == "root"
    # USER is stateful and the LAST one wins, both for later RUNs and the image.
    steps = [("USER", "65532:65532"), ("RUN", "guard"), ("USER", "root"),
             ("RUN", "seal"), ("USER", "65532:65532")]
    assert _user_in_effect(steps, 1) == "65532:65532"   # at the guard
    assert _user_in_effect(steps, 4) == "root"          # at the pinned RUN
    assert _user_in_effect(steps) == "65532:65532"      # the container's uid

    # A guard that runs as root on an image that drops privilege proves nothing.
    with pytest.raises(AssertionError, match="runs as 'root' but the container"):
        _assert_guard_runs_as_the_runtime_identity(
            "probe", [("RUN", "guard"), ("USER", "10002:10002")], 0)
    # ...and one that runs as the runtime uid is accepted, including when the
    # image steps back to root AFTER the guard and then returns.
    assert _assert_guard_runs_as_the_runtime_identity("probe", steps, 1) == "65532:65532"

    # The `-r`/`-x` clauses, and the derivation of which file needs `-x`.
    assert _survival_guard_argv(["/a"], readable=True) == [
        "/bin/sh", "-c", "test -f /a && test -s /a && test -r /a"]
    assert _survival_guard_argv(["/a"], readable=True, executable="/a") == [
        "/bin/sh", "-c", "test -f /a && test -s /a && test -r /a && test -x /a"]
    # consume_arg appends a per-commit ARG reference (canonical-worker) so the
    # guard may legally sit below that ARG; it is `test -n`, appended last.
    assert _survival_guard_argv(
        ["/a"], readable=True, consume_arg="LEAF_SOURCE_SHA") == [
        "/bin/sh", "-c",
        'test -f /a && test -s /a && test -r /a && test -n "${LEAF_SOURCE_SHA}"']
    # argv[0] of the exec-form CMD, but ONLY when it is a guarded script.
    assert _cmd_executable([("CMD", '["/app/scripts/s.sh"]')], ["/app/scripts/s.sh"]) == (
        "/app/scripts/s.sh")
    assert _cmd_executable([("CMD", '["python", "w.py"]')], ["/app/scripts/s.sh"]) is None
    # A shell-form CMD hides which file is executed, so it is refused.
    with pytest.raises(AssertionError, match="not exec form"):
        _cmd_executable([("CMD", "/app/scripts/s.sh")], ["/app/scripts/s.sh"])

    # The one exemption to the destination-above-a-script refusal is the
    # narrowest decidable shape, and each of the four defects that killed the
    # earlier adjudicating version must stay excluded by construction.
    assert _writes_only_named_files(
        "COPY", [], ["harness/package.json", "harness/package-lock.json"])
    # ADD extracts a local archive detected by CONTENT.
    assert not _writes_only_named_files("ADD", [], ["harness/package.json"])
    # --from= sources a tree that is not this repository.
    assert not _writes_only_named_files(
        "COPY", ["--from=build"], ["harness/package.json"])
    # A directory source recurses.
    assert not _writes_only_named_files("COPY", [], ["harness/scripts"])
    # A glob's match set is not decidable here, and can match a directory.
    assert not _writes_only_named_files("COPY", [], ["harness/*.json"])
    # An ABSENT source fails closed: unknown means refuse, never accept.
    assert not _writes_only_named_files("COPY", [], ["harness/no-such-file.json"])

    # The dockerignore subset must recognise this repository's real rules, or
    # the directory form would demand files the build context never delivers.
    assert _is_dockerignored("harness/scripts/debug.log")
    assert _is_dockerignored("harness/scripts/node_modules")
    assert not _is_dockerignored("harness/scripts/start-harness.sh")


def test_every_image_asserts_its_shipped_scripts_at_build_time():
    """The destruction class, closed for the three images that still had it open.

    deploy/Dockerfile.app has carried this guard since #503. The other three
    shipped operator-reachable scripts with the identical hole and nothing
    watching: canonical-container-smoke.py (the worker's containerised smoke),
    e2b-tool-exec.mjs (the broker's micro-VM helper, named by LEAF_E2B_HELPER),
    and start-harness.sh (the harness image's literal CMD). An instruction that
    deleted or truncated any of them left every static check green, because the
    COPY line is still there to read; the failure surfaced in an operator
    cutover instead.

    Same spelling for all four via `_survival_guard_argv`: `test -f && test -s`
    per shipped script, plus `test -r` as the runtime uid (and `test -x` where the
    CMD is a guarded script), in EXEC form. app/broker/harness place it ABOVE
    their per-commit ARG so it stays cached; canonical-worker cannot -- its two
    root RUNs are forced below that ARG -- so its guard runs LAST and carries one
    extra clause, `test -n "${LEAF_SOURCE_SHA}"`, to consume the ARG it now sits
    below (#514 round 3/4). Derived from each Dockerfile's own COPY map rather
    than a curated list, so a script added later is covered the day it is copied
    -- and for the harness, whose COPY is a DIRECTORY, that means the day it lands
    in harness/scripts/.

    WHAT A DAEMON HAS ACTUALLY RUN. The guard MECHANICS are now daemon-proven
    (Docker server 29.6.2, #514 round 4): a Dockerfile reproducing
    canonical-worker's structure -- guard LAST, as uid 65532, after two root RUNs
    -- builds green unmodified, and FAILS at the guard step under a root
    `chmod 700 /app/scripts` in the window, under `SHELL ["/bin/true"]` above the
    guard plus a deletion, and under a `: >` truncation. That proves the runtime
    identity, exec-form SHELL-immunity, and the round-4 post-root-window closure
    on a real engine. What was NOT built is the BYTE-EXACT production image: its
    attestation RUN needs the exact external solver context, not reproduced here,
    so the stubbed build swaps that RUN for a trivial root RUN. Do not read this
    as a full production build-proof; the byte-exact PASS/FAIL paths of the real
    four images still rest on Docker's documented rules (SHELL does not affect the
    exec form; `test -f`/`-s`/`-r` do not change meaning with the interpreter; an
    exec-form RUN that exits nonzero fails the build). Do not trust a CACHED layer
    as proof: it only shows some earlier identical layer succeeded.

    WHAT IS STATICALLY PROVEN here, AS OPPOSED TO at build time: every assertion
    was mutation-checked. The exec->shell downgrade, an interpreter swap, dropping
    `test -s` or `test -r`, deleting the guard, adding a RUN after it, a uid
    rename, and adding a file to harness/scripts/ each turn this test red; for
    canonical-worker specifically, moving the guard above its root window (so a
    RUN follows it) turns it red, and dropping its ARG-consuming clause turns both
    this test and the build-workflow gate red. What this STATIC test does NOT
    catch is a `chmod`, deletion or truncation of a guarded path: those pass every
    text assertion and are caught at BUILD time by the guard RUN itself
    (daemon-proven above), never here.

    NOT covered by anything: content. `-s` closes the zero-byte case and NOTHING
    WIDER -- a one-byte overwrite is a valid program that exits 0. And no BUILD-
    time guard binds RUNTIME behaviour, so a `HEALTHCHECK CMD rm -rf
    /app/scripts/...` passes the build and deletes the script seconds into
    container life. Both are review classes, not gates.

    ONE MORE REVIEW CLASS, plus one this design CLOSED that used to sit here:

      * CLOSED -- build-time reachability as the dropped uid. Each image now
        declares its runtime USER ABOVE its guard, so the guard runs as that uid
        and its `test -r` (and `test -x` on the harness CMD) observe the real
        uid, gid and directory traversal; `_assert_guard_runs_as_the_runtime_identity`
        enforces the pairing. Nothing reads a mode's spelling, so the six
        evasions that beat the mode-evaluating predecessor and the two
        (`install --mode=`, `c''hmod`) that beat its byte-pinning successor are
        closed by construction. For canonical-worker, whose two root RUNs are
        forced below the per-commit ARG, the guard runs LAST -- after that root
        window -- so a `chmod` of /app/scripts made by the mutable Python those
        RUNs import is caught as well (the gap sol-critic found in #514 round 3).
        What remains uncovered is RUNTIME, not build time: a `HEALTHCHECK`/`CMD`
        that re-permissions the path seconds into container life, named above.
      * A SYMLINKED DESTINATION. Every destination check in this module,
        including `_copied_scripts`, compares the destination as WRITTEN, while
        ordinary COPY follows a destination symlink. So
            RUN ln -s /app/scripts /app/alias
            COPY harness/package.json /app/alias/start-harness.sh
        overwrites a guarded script with a non-empty file, and `-f`/`-s` both
        still pass. Confirmed: that destination is neither equal to, nor an
        ancestor of, any guarded target, so the equality and `_contains` checks
        BOTH miss it independently -- it is a property of textual destination
        comparison, not of the `_writes_only_named_files` exemption, and
        removing that exemption does not close it. Closing it needs the build
        filesystem, which is the interpreter this module has twice refused to
        become. `COPY --link` cannot follow such a symlink, so requiring it is
        the shape of a real fix if this class ever needs gating.
    """
    for image, (motivating, extra_allowed, allowed_runs, expected_uid,
                consume_arg) in _GUARDED_IMAGES.items():
        path = f"deploy/Dockerfile.{image}"
        shipped = _shipped_scripts(path)

        # ANTI-VACUITY, and the assertion that matters most here: a parse that
        # silently stopped seeing this image's COPY would yield an empty map, an
        # empty guard command, and a green test over an image asserting nothing.
        # Naming the script each guard exists for cannot fail that way.
        assert motivating in shipped.values(), (
            f"{path}: {motivating} is the operator-reachable script this guard "
            f"exists for, and the COPY map does not reach it: {shipped}"
        )

        instructions = _shipped_instructions(path)
        # These images all drop privilege, so their guards must prove READABLE
        # (and EXECUTABLE for an image whose CMD is one of the scripts) as the
        # runtime uid, not merely present and non-empty as root.
        runtime = _user_in_effect(instructions)
        assert runtime != "root", (
            f"{path}: this image no longer drops privilege. The guard's `-r`/"
            "`-x` clauses were added because it did; re-derive them before "
            "relaxing this."
        )
        # PIN THE UID STRUCTURALLY, from the INSTRUCTIONS rather than the file
        # text. The sibling tests pin it as a substring of the whole Dockerfile
        # (`assert "USER 10001:10001" in dockerfile`), and moving USER above the
        # guard meant writing a comment about the move -- at which point a
        # comment mentioning the literal satisfied that pin ON ITS OWN, while the
        # real instruction could say anything. Found by mutation, not review: a
        # uid rename stayed green across the whole file. `_user_in_effect` reads
        # parsed instructions, where a comment cannot reach.
        assert runtime == expected_uid, (
            f"{path}: the container runs as {runtime!r}, not the pinned "
            f"{expected_uid!r}. The deploy manifests and compose files bind to "
            "that uid; changing it is a deliberate act, not a side effect of "
            "moving USER above the guard."
        )
        guard_index = _assert_one_survival_guard(
            path, instructions, shipped.values(),
            _CANNOT_REMOVE | extra_allowed, allowed_runs,
            readable=True,
            executable=_cmd_executable(instructions, shipped.values()),
            consume_arg=consume_arg,
        )
        _assert_guard_runs_as_the_runtime_identity(path, instructions, guard_index)

    # The harness ships a DIRECTORY, so its guarded set is that directory's
    # contents. Re-read independently of the helper: were the directory form
    # silently skipped, the map would be empty and this comparison would fail
    # rather than vacuously pass.
    harness = _shipped_scripts("deploy/Dockerfile.harness")
    on_disk = {
        entry.name for entry in (REPO_ROOT / "harness" / "scripts").iterdir()
        if entry.is_file()
    }
    assert on_disk, "harness/scripts/ is empty"
    assert {PurePosixPath(target).name for target in harness.values()} == on_disk, (
        "the harness guard does not cover every file its directory COPY ships: "
        f"guarded={sorted(harness.values())}, on disk={sorted(on_disk)}"
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
