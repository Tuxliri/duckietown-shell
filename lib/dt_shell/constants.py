# -*- coding: utf-8 -*-
import os

from typing import Optional

import termcolor

from . import __version__


DEFAULT_PROFILE = "default"
DEFAULT_ROOT = "~/.duckietown/shell/{profile}"


class DTShellConstants:
    PROFILE = os.environ.get("DTSHELL_PROFILE", DEFAULT_PROFILE)
    ROOT = os.path.expanduser(os.environ.get("DTSHELL_ROOT", DEFAULT_ROOT.format(profile=PROFILE)))
    ENV_COMMANDS = "DTSHELL_COMMANDS"

    DT1_TOKEN_CONFIG_KEY = "token_dt1"
    CONFIG_DOCKER_USERNAME = "docker_username"
    CONFIG_DOCKER_PASSWORD = "docker_password"
    CONFIG_DUCKIETOWN_VERSION = "duckietown_version"
    CONFIG_DOCKER_CREDENTIALS = "docker_credentials"


ALLOWED_BRANCHES = ["ente(-[\w]+)?", "daffy(-[\w]+)?", "master19(-[\w]+)?", "devel(-[\w]+)?"]

DEBUG = False

CHECK_CMDS_UPDATE_MINS = 5

DNAME = "Duckietown Shell"


def INTRO(extra: Optional[str] = None) -> str:
    return """

Welcome to the interactive {Duckietown} ({version}).
{extra}
Type "help" or "?" to list commands.

""".format(
        Duckietown=termcolor.colored(DNAME, "yellow", attrs=["bold"]), version=__version__, extra=extra or ""
    ).lstrip()
