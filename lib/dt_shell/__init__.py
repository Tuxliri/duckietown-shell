# -*- coding: utf-8 -*-
import logging
from typing import Optional

logging.basicConfig()

dtslogger = logging.getLogger("dts")
dtslogger.setLevel(logging.INFO)

__version__ = "5.5.8"


dtslogger.debug(f"duckietown-shell {__version__}")

import sys

if sys.version_info < (3, 6):
    msg = f"! duckietown-shell works with Python 3.6 and later !.\nDetected {sys.version}."
    logging.error(msg)
    sys.exit(2)

from .exceptions import ConfigInvalid, ConfigNotPresent

from .cli import DTShell
from .logging import dts_print

from .commands import DTCommandAbs
from .commands import DTCommandPlaceholder
from .main import cli_main
from .exceptions import *

from .main import OtherVersions

# singleton
shell: Optional[DTShell] = None
