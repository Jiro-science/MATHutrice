import json
import shlex
import subprocess
import sys

data = json.load(sys.stdin)
command = data.get("tool_input", {}).get("command", "")

SHELL_OPERATORS = {"&&", "||", ";", "|"}


def find_git_push_arg_lists(command: str) -> list[list[str]]:
    """Tokenize the shell command and return the argument list following
    each real `git push` invocation (skipping global git flags before the
    subcommand), so a `push`/`main` mention inside an unrelated quoted
    string (e.g. a commit message) can't trigger a false match."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    arg_lists = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "git":
            j = i + 1
            while j < len(tokens) and tokens[j].startswith("-"):
                j += 1
            if j < len(tokens) and tokens[j] == "push":
                end = j + 1
                while end < len(tokens) and tokens[end] not in SHELL_OPERATORS:
                    end += 1
                arg_lists.append(tokens[j + 1:end])
                i = end
                continue
        i += 1
    return arg_lists


def refspec_target(refspec: str) -> str:
    """Destination branch name a push refspec resolves to on the remote."""
    refspec = refspec.lstrip("+")  # drop the force-push prefix
    if ":" in refspec:
        src, dst = refspec.split(":", 1)
        return dst or src
    return refspec


push_arg_lists = find_git_push_arg_lists(command)

if push_arg_lists:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True
    )
    current_branch = result.stdout.strip()
    on_main = current_branch == "main"

    for args in push_arg_lists:
        positional = [a for a in args if not a.startswith("-")]
        explicit_refspecs = positional[1:]  # positional[0], if any, is the remote

        if explicit_refspecs:
            # An explicit target was given (e.g. `git push origin main-backup`):
            # only block if one of the refspecs actually resolves to main.
            should_block = any(refspec_target(r) == "main" for r in explicit_refspecs)
        else:
            # No explicit refspec (`git push` or `git push <remote>`): this
            # pushes the current branch implicitly, so it matters if we're on main.
            should_block = on_main

        if should_block:
            print(
                "BLOCKED: direct push to 'main' is not allowed. "
                "Create a feature branch and open a pull request instead.",
                file=sys.stderr
            )
            sys.exit(2)

sys.exit(0)
