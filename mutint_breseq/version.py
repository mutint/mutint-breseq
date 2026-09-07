"""The version of mutint-breseq itself.

An installed app contributes a version by exposing `__version__` from a `version`
submodule -- no registration, because the app being installed is already the
statement that it is part of this project. `./mutint version` finds it that way.
`/about` does not, which is why apps.py passes it to `register_about_section` as
well: the two surfaces are independent.

`NAME` is what `./mutint version` prints and what `--component` matches. Omitted,
both fall back to the Django app label -- `mutint_breseq` -- so this is what makes a
component name itself after its repository, the way mutint-core does.

Bump it with `./mutint version --bump patch --component mutint-breseq`, and tag
the release commit `v<version>` to match.
"""

NAME = "mutint-breseq"

__version__ = "0.0.1"
