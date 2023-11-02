# -*- coding: utf-8 -*-

import argparse
import atexit
import dataclasses
import inspect
import os
import random
import signal
import sys
import time
import traceback
import types
from cmd import Cmd
from dataclasses import dataclass
from enum import Enum
from math import floor
from typing import List, Optional, Tuple, Union, Type, Dict, Callable
from typing import Mapping, Sequence

import questionary
from dt_authentication import DuckietownToken
from pyfiglet import Figlet
from questionary import Choice
from termcolor import termcolor

from . import __version__, logger, compatibility
from .checks.version import check_for_updates
from .commands import DTCommandAbs, CommandDescriptor, DTCommandPlaceholder, DTCommandConfigurationAbs, \
    CommandSet, NoOpCommand
from .commands.importer import import_command, import_configuration
from .compatibility.migrations import \
    migrate_distro, \
    needs_migrate_docker_credentials, migrate_docker_credentials, \
    needs_migrate_token_dt1, migrate_token_dt1, \
    needs_migrate_secrets, migrate_secrets, mark_docker_credentials_migrated, \
    mark_token_dt1_migrated, mark_secrets_migrated, needs_migrations, mark_all_migrated
from .constants import DNAME, KNOWN_DISTRIBUTIONS, SUGGESTED_DISTRIBUTION, EMBEDDED_COMMAND_SET_NAME, \
    DB_BILLBOARDS, DB_UPDATES_CHECK, CHECK_BILLBOARD_UPDATE_SECS
from .constants import DTShellConstants, IGNORE_ENVIRONMENTS, DB_SETTINGS, DB_PROFILES, CHECK_SHELL_TOKEN_SECS
from .database import DTShellDatabase
from .environments import ShellCommandEnvironmentAbs, DEFAULT_COMMAND_ENVIRONMENT
from .exceptions import UserError, NotFound, CommandNotFound, CommandsLoadingException, UserAborted, \
    ConfigNotPresent
from .logging import dts_print
from .profile import ShellProfile
from .utils import text_justify, text_distribute, cli_style, indent_block, ensure_bash_completion_installed

BILLBOARDS_VERSION: str = "v1"

CommandName = str
CommandsTree = Dict[CommandName, Union[None, Mapping[CommandName, dict], CommandDescriptor]]


@dataclass
class CLIOptions:
    debug: bool = False
    verbose: bool = False
    quiet: bool = False
    complete: bool = False


def get_cli_options(args: List[str]) -> Tuple[CLIOptions, List[str]]:
    """Returns cli options plus other arguments for the commands."""

    if args and not args[0].startswith("-"):
        return CLIOptions(), args

    # find first non-option word
    i: int = 0
    for w in args:
        if w.startswith("-"):
            i += 1
        else:
            break

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="More debug information"
    )
    parser.add_argument(
        "-vv", "--verbose",
        action="store_true",
        default=False,
        help="More debug information from the shell"
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        default=False,
        help="Quiet execution"
    )

    if "--complete" in args[:i]:
        parser.add_argument(
            "--complete",
            action="store_true",
            default=False,
            help="Execute command completion",
        )

    parsed, others = parser.parse_known_args(args)

    return CLIOptions(**parsed.__dict__), others


prompt = "dts> "


class ShellSettings(DTShellDatabase):

    @classmethod
    def load(cls, location: Optional[str] = None):
        return DTShellDatabase.open(DB_SETTINGS, location=location)

    @property
    def check_for_updates(self) -> bool:
        return self.get("check_for_updates", True)

    @check_for_updates.setter
    def check_for_updates(self, value: bool):
        assert isinstance(value, bool)
        self.set("check_for_updates", value)

    @property
    def profile(self) -> Optional[str]:
        return self.get("profile", None)

    @profile.setter
    def profile(self, value: str):
        assert isinstance(value, str)
        self.set("profile", value)

    @property
    def show_billboards(self) -> bool:
        return self.get("show_billboards", True)

    @show_billboards.setter
    def show_billboards(self, value: bool):
        assert isinstance(value, bool)
        self.set("show_billboards", value)


class EventType(Enum):
    START = "start"
    PRE_COMMAND_IMPORT = "pre-command-import"
    POST_COMMAND_IMPORT = "post-command-import"
    KEYBOARD_INTERRUPT = "keyboard-interrupt"
    SHUTDOWN = "shutdown"


@dataclasses.dataclass
class Event:
    type: EventType
    origin: str
    time: float = dataclasses.field(default_factory=time.time)


class DTShell(Cmd):
    commands: CommandsTree = {}
    core_commands: List[CommandName] = [
        "commands",
        "install",
        "uninstall",
        "update",
        "version",
        "exit",
        "help",
    ]

    # tree of commands once loaded
    include: types.SimpleNamespace

    def __init__(self,
                 skeleton: bool = False,
                 readonly: bool = False,
                 banner: bool = True,
                 billboard: bool = True):
        # errors while loading end up in here
        self._errors_loading: List[str] = []

        # updates check database
        self.updates_check_db: DTShellDatabase[float] = DTShellDatabase.open(DB_UPDATES_CHECK)

        # namespace will contain the map to the improted commands
        DTShell.include = types.SimpleNamespace()
        self._event_handlers: Dict[EventType, List[Callable]] = {
            EventType.START: [
                self._run_background_tasks
            ],
            EventType.PRE_COMMAND_IMPORT: [],
            EventType.POST_COMMAND_IMPORT: [],
            EventType.KEYBOARD_INTERRUPT: [
                self._on_keyboard_interrupt_event
            ],
            EventType.SHUTDOWN: [
                self._on_shutdown_event
            ],
        }

        # set all databases to readonly if needed
        DTShellDatabase.global_readonly = readonly

        # open databases
        self._db_profiles: DTShellDatabase = DTShellDatabase.open(DB_PROFILES, readonly=readonly)
        self._db_settings: ShellSettings = ShellSettings.open(DB_SETTINGS, readonly=readonly)

        # load current profile
        self._profile: ShellProfile = ShellProfile(self.settings.profile, readonly=readonly) \
            if self.settings.profile else None

        # check shell token
        if not skeleton and not readonly:
            self._check_shell_token()

        # start event
        self._trigger_event(Event(EventType.START, "shell"))

        # get billboard to show (if any)
        bboard: Optional[str] = None
        if billboard and self.settings.show_billboards:
            bboard = self.get_billboard()

        # print banner
        if banner:
            self._show_banner(profile=self._profile, billboard=bboard)

        # make sure the bash completion script is installed
        if not readonly:
            ensure_bash_completion_installed()

        # check if we configure the shell by migrating an old profile
        self._attempt_migrations(readonly)

        # make sure the shell is configured
        self._configure(readonly)

        # make sure the profile is configured
        self._profile.configure(readonly)

        # in readonly mode we stop right here if we don't have a profile
        if readonly and self._profile is None:
            return

        # make sure the shell is properly configured
        assert self._profile is not None

        # update shell constants
        DTShellConstants.PROFILE = self._profile
        DTShellConstants.ROOT = self._profile.path

        # check for updates
        if not readonly and not skeleton and self.settings.check_for_updates:
            check_for_updates()

        # add command set path to PYTHONPATH
        for cs in self.command_sets:
            for path in [cs.path, os.path.join(cs.path, "lib")]:
                # add new path to PYTHONPATH for this session
                path: str = os.path.abspath(path)
                sys.path.insert(0, path)
                logger.debug(f"Path '{path}' added to PYTHONPATH")

        # call super constructor
        super(DTShell, self).__init__()

        # remove the char `-` from the list of word separators, this allows us to suggest flags
        if self.use_rawinput and self.completekey:
            import readline
            readline.set_completer_delims(readline.get_completer_delims().replace("-", "", 1))

        # check for updates (if needed)
        if not readonly:
            for cs in self.command_sets:
                # Do not check it if we are using custom commands (leave-alone)
                if not cs.leave_alone:
                    cs.update()

        # pre-import event
        self._trigger_event(Event(EventType.PRE_COMMAND_IMPORT, "shell"))

        # load commands
        self.load_commands(skeleton)

        # make sure nobody is importing command implementations when in skeleton mode
        if skeleton:
            terminate: bool = False
            for subclass in DTCommandAbs.__subclasses__():
                if subclass in [DTCommandPlaceholder, NoOpCommand]:
                    continue
                origin_fpath: str = inspect.getfile(subclass)
                logger.error(f"The file '{origin_fpath}' was loaded while the shell run in skeleton "
                             f"mode. This is not allowed, command implementation should never be manually "
                             f"imported in the command set files.")
                terminate = True
            if terminate:
                raise UserError("Some command implementations were imported while running in skeleton mode.")

        # post-import event
        self._trigger_event(Event(EventType.POST_COMMAND_IMPORT, "shell"))

        # apply backward-compatibility edits
        compatibility.apply(self)

        # register SIGINT handler
        signal.signal(
            signal.SIGINT,
            lambda sig, frame: self._trigger_event(Event(EventType.KEYBOARD_INTERRUPT, "user"))
        )

        # register at-exit (we use a lambda so that the event is created at the proper time)
        atexit.register(lambda: self._trigger_event(Event(EventType.SHUTDOWN, "shell")))

    @property
    def profile(self) -> Optional[ShellProfile]:
        return self._profile

    @property
    def settings(self) -> ShellSettings:
        return self._db_settings

    @property
    def profiles(self) -> DTShellDatabase:
        return self._db_profiles

    @property
    def command_sets(self) -> List[CommandSet]:
        return self._profile.command_sets

    @property
    def commands_tree(self) -> List[CommandSet]:
        return self._profile.command_sets

    def command_set(self, name: str) -> CommandSet:
        """
        Returns the CommandSet with the given name if found installed. Raises NotFound otherwise.
        """
        for cs in self.command_sets:
            if cs.name == name:
                return cs
        raise NotFound(f"Command set '{name}' not found")

    def postcmd(self, stop, line):
        if len(line.strip()) > 0:
            print("")

    def default(self, line: str) -> None:
        # TODO: suggest possible commands as well
        dts_print(f"Unknown syntax:\n\n\t\tdts {line}\n", color="red")

    def emptyline(self):
        pass

    def complete(self, text, state):
        res = super(DTShell, self).complete(text, state)
        if res is not None:
            res += " "
        return res

    def on_event(self, event: EventType, handler: Callable[[Event], None]):
        if event not in self._event_handlers:
            raise KeyError(f"Event name '{event.name}' not recognized. "
                           f"Known events are: {list(self._event_handlers.keys())}")
        self._event_handlers[event].append(handler)

    def on_start(self, handler: Callable[[Event], None]):
        self.on_event(EventType.SHUTDOWN, handler)

    def on_shutdown(self, handler: Callable[[Event], None]):
        self.on_event(EventType.SHUTDOWN, handler)

    def on_keyboard_interrupt(self, handler: Callable[[Event], None]):
        self.on_event(EventType.KEYBOARD_INTERRUPT, handler)

    def needs_update(self, key: str, period: float, default: bool = True) -> bool:
        # read record
        try:
            last_time_checked: float = self.updates_check_db.get(key)
        except DTShellDatabase.NotFound:
            return default
        # check time
        return time.time() - last_time_checked > period

    def mark_updated(self, key: str, when: float = None):
        # update record
        self.updates_check_db.set(key, when if when is not None else time.time())

    def _run_background_tasks(self, event: Event):
        if event.type is EventType.START:
            # update billboards
            if self.needs_update("billboards", CHECK_BILLBOARD_UPDATE_SECS):
                from .tasks import UpdateBillboardsTask
                UpdateBillboardsTask(self).start()
            # update shell token
            if self.profile is not None:
                if self.profile.needs_update("shell-token", CHECK_SHELL_TOKEN_SECS):
                    from .tasks import ShellTokenTask
                    ShellTokenTask(self).start()

    def _on_keyboard_interrupt_event(self, event: Event):
        pass

    def _on_shutdown_event(self, event: Event):
        pass

    def _trigger_event(self, event: Event):
        logger.debug(f"{event.origin.title()} triggered the event '{event.type.name}'")
        for cb in self._event_handlers[event.type]:
            try:
                cb(event)
            except Exception:
                traceback.print_exc()
                logger.error(f"An handler for the event '{event.type.name}' failed its execution. "
                             f"The exception is printed to screen.")

    def _attempt_migrations(self, readonly: bool = False):
        # make sure we need migrations
        if not needs_migrations():
            return
        elif readonly:
            raise ConfigNotPresent()

        asked_confirmation: bool = False
        # get the name of the old profile
        distro: Optional[str] = migrate_distro(dryrun=True)
        # if we didn't have a version set in the old format then we have nothing to migrate
        if distro is None:
            # we mark everything as migrated, so we don't ask again
            mark_all_migrated()
            return

        def _ask_confirmation() -> bool:
            print(f"The Duckietown shell now uses a new profile format. "
                  f"An old profile '{distro}' was found on disk. "
                  f"We can automatically migrate all your preferences, credentials and tokens into the "
                  f"new profile format.")
            # ask the user whether to continue with the migration
            return questionary.confirm(
                "Do you want to migrate your old profile?",
                auto_enter=True
            ).unsafe_ask()

        # try to migrate profile/distro
        if self.profile is None:
            granted: bool = _ask_confirmation()
            asked_confirmation = True
            if not granted:
                print("A fresh start, we can work with that.")
                # we mark everything as migrated, so we don't ask again
                mark_all_migrated()
                return
            # we are migrating
            distro: str = migrate_distro(dryrun=True)
            # make new profile
            logger.info(f"Migrating profile '{distro}'...")
            self._profile = ShellProfile(name=distro)
            # set the new profile as the profile to load at the next launch
            self.settings.profile = distro

        # by now we must have a profile
        assert self.profile is not None

        # try to migrate dt1 token
        if needs_migrate_token_dt1():
            migrate: bool = True
            if not asked_confirmation:
                migrate = _ask_confirmation()
                asked_confirmation = True
            # migrate?
            if migrate:
                token: Optional[str] = migrate_token_dt1(self.profile)
                if token is not None:
                    logger.info(f"Migrated: Tokens")
                # mark it as migrated, so we don't ask again
                mark_token_dt1_migrated()

        # try to migrate docker credentials
        if needs_migrate_docker_credentials():
            migrate: bool = True
            if not asked_confirmation:
                migrate = _ask_confirmation()
                asked_confirmation = True
            # migrate?
            if migrate:
                no_migrated: int = migrate_docker_credentials(self.profile)
                logger.info(f"Migrated: {no_migrated} Docker credentials")
                # mark it as migrated, so we don't ask again
                mark_docker_credentials_migrated()

        # try to migrate secrets
        if needs_migrate_secrets():
            migrate: bool = True
            if not asked_confirmation:
                migrate = _ask_confirmation()
                # noinspection PyUnusedLocal
                asked_confirmation = True
            # migrate?
            if migrate:
                migrate_secrets(self.profile)
                logger.info(f"Migrated: Other secrets")
                # mark it as migrated, so we don't ask again
                mark_secrets_migrated()

        # complete profile configuration
        self.profile.configure()

    def load_commands(self, skeleton: bool):
        # rediscover commands
        self.commands = {}
        for cs in self.command_sets:
            # run command set init script
            if not skeleton:
                cs.init()

            # load commands from disk
            for cmd, subcmds in cs.commands.items():
                # noinspection PyTypeChecker
                self._load_command_subtree(cs, "", cmd, subcmds, 0, skeleton)

            # add commands to the list of commands
            self.commands.update(cs.commands)

        if len(self.commands) <= 0:
            logger.error("No commands found.")
            self.commands = {}

        if self._errors_loading:
            sep = "-" * 128
            msg = f"\n\n\n!   Could not load commands. Detailed error messages are printed below.\n" + \
                  indent_block(
                      f"\n\n{sep}\n\n\n" +
                      ("\n\n" + sep + "\n\n\n").join(self._errors_loading) +
                      f"\n\n{sep}\n\n"
                  ) + \
                  f"\n\n!   Could not load commands. Detailed error messages are printed above.\n\n"
            logger.error(msg)
            raise CommandsLoadingException("Some commands could not be loaded. Detailed error messages are "
                                           "reported above.")

    def reload_commands(self, skeleton: bool):
        # remove installed commands
        installed_commands = self.commands.keys()
        for command in installed_commands:
            for a in ["do_", "complete_", "help_"]:
                if hasattr(DTShell, a + command):
                    delattr(DTShell, a + command)
        # rediscover commands
        self.load_commands(skeleton)

    def _load_command_subtree(
        self,
        command_set: CommandSet,
        package: str,
        command: str,
        sub_commands: Union[None, Mapping[str, object], CommandDescriptor],
        lvl: int,
        skeleton: bool,
    ) -> Optional[Type[DTCommandAbs]]:
        # make a new (temporary) class
        class klass(DTCommandPlaceholder):
            pass
        selector: str = f"{package}{command}"
        configuration: Type[DTCommandConfigurationAbs] = import_configuration(command_set, selector)

        if isinstance(sub_commands, CommandDescriptor):
            descriptor: CommandDescriptor = sub_commands

            # load command configuration
            descriptor.configuration = configuration

            # if we ignore environments, we assume default global environment for every command
            environment: ShellCommandEnvironmentAbs
            if IGNORE_ENVIRONMENTS:
                environment = DEFAULT_COMMAND_ENVIRONMENT
            else:
                # figure out the environment for this command
                environment = descriptor.configuration.environment()
                if environment is None:
                    # revert to command set's default environment
                    environment = command_set.configuration.default_environment()
                if environment is None:
                    # use default environment
                    environment = DEFAULT_COMMAND_ENVIRONMENT

            # add environment to command's descriptor
            descriptor.environment = environment

            # import class only if this is the environment in which the commands will run
            if not skeleton:
                try:
                    klass = import_command(command_set, descriptor.path)
                except UserError:
                    raise
                except (UserAborted, KeyboardInterrupt):
                    raise
                except ModuleNotFoundError:
                    lines: List[str] = []
                    cs_path: str = os.path.abspath(os.path.realpath(command_set.path))
                    # check PYTHONPATH
                    found: bool = False
                    lines += ["\tPYTHONPATH: ["]
                    for p in sys.path:
                        p_path: str = os.path.abspath(os.path.realpath(p))
                        if p_path == cs_path:
                            found = True
                        lines += [f"\t\t'{p_path}'"]
                    lines += ["\t]"]
                    lines += [f"\t- dir[{cs_path}] in PYTHONPATH: {found}"]
                    # module already loaded?
                    m: str = ""
                    for p in selector.split("."):
                        m = f"{m}.{p}".lstrip(".")
                        mod = sys.modules.get(m, None)
                        if mod:
                            lines.append(f"\t- module[{m}] already loaded: True; {dir(mod)}")
                        else:
                            lines.append(f"\t- module[{m}] already loaded: False")

                    # check all __init__ files
                    fpath: str = os.path.join(cs_path)
                    for p in selector.split("."):
                        fpath = os.path.join(fpath, p)
                        init_fpath = os.path.join(fpath, "__init__.py")
                        lines.append(f"\t- file[{init_fpath}] exists: {os.path.isfile(init_fpath)}")
                    # compile details
                    details: str = "\n".join(lines)
                    msg = f"The command '{selector.replace('.', '/')}' could not be imported." \
                          f"\n\n{details}\n\n" \
                          f"{traceback.format_exc()}"
                    self._errors_loading.append(msg)
                except BaseException:
                    se = traceback.format_exc()
                    msg = (
                        f"Cannot load command class {descriptor.selector}.command.DTCommand "
                        f"(package={package}, command={command}):\n\n{se}"
                    )
                    self._errors_loading.append(msg)
                    return

            # link descriptor <-> command
            descriptor.command = klass
            klass.descriptor = descriptor
            # add loaded class to DTShell.include.<cmd_path>
            klass_path = [p for p in package.split(".") if len(p)]
            base = DTShell.include
            for p in klass_path:
                if not hasattr(base, p):
                    setattr(base, p, types.SimpleNamespace())
                base = getattr(base, p)
            setattr(base, command, klass)

        # give command its own info
        klass.name = command
        klass.level = lvl
        klass.parser = configuration.parser()
        # initialize list of subcommands
        klass.commands = {}

        # attach first-level commands to the shell
        if lvl == 0:
            do_command = getattr(klass, "do_command")
            get_command = getattr(klass, "get_command")
            complete_command = getattr(klass, "complete_command")
            help_command = getattr(klass, "help_command")
            # wrap [klass, function] around a lambda function
            do_command_lam = lambda s, w: do_command(klass, s, w)
            get_command_lam = lambda s, w: get_command(klass, s, w)
            complete_command_lam = lambda s, w, l, i, _: complete_command(klass, s, w, l, i, _)
            help_command_lam = lambda s: help_command(klass, s)
            # add functions do_* and complete_* to the shell
            for command_name in [command] + configuration.aliases():
                if DTShellConstants.VERBOSE:
                    logger.debug(f"Attaching root command '{command_name}' to shell")
                setattr(DTShell, "do_" + command_name, do_command_lam)
                setattr(DTShell, "get_" + command_name, get_command_lam)
                setattr(DTShell, "complete_" + command_name, complete_command_lam)
                setattr(DTShell, "help_" + command_name, help_command_lam)

        # stop recursion if there is no subcommand
        if sub_commands is None:
            return

        # load sub-commands
        if isinstance(sub_commands, dict):
            for cmd, subcmds in sub_commands.items():
                logger.debug("Searching %s at level %d" % (package + command + ".*", lvl))
                # noinspection PyTypeChecker
                kl = self._load_command_subtree(
                    command_set, package + command + ".", cmd, subcmds, lvl + 1, skeleton
                )
                if kl is not None:
                    klass.commands[cmd] = kl

        # return class for this command
        return klass

    def get_command(self, line) -> CommandDescriptor:
        """
        Interpret the argument and looks for the command that would be executed by the function onemcd(line).

        """
        cmd, arg, line = self.parseline(line)
        if not line or cmd is None or cmd == '':
            raise CommandNotFound(last_matched=None, remaining=line.split(" "))
        else:
            try:
                get_command = getattr(self, 'get_' + cmd)
            except AttributeError:
                raise CommandNotFound(last_matched=None, remaining=line.split(" "))
            # find command recursively down the tree
            cmd, _ = get_command(arg)
            return cmd

    # noinspection PyMethodMayBeStatic
    def sprint(self, msg: str, color: Optional[str] = None, attrs: Sequence[str] = None) -> None:
        attrs = attrs or []
        return dts_print(msg=msg, color=color, attrs=attrs)

    @staticmethod
    def get_billboard() -> Optional[str]:
        # get billboards from the local database
        db: DTShellDatabase = DTShellDatabase.open(DB_BILLBOARDS)
        # collect billboards names based on priority
        names: List[str] = []
        for name, billboard in db.items():
            names.extend([name] * (billboard["priority"] + 1))
        # no billboards?
        if not names:
            return None
        # pick one source at random
        name: str = random.choice(names)
        # ---
        return db.get(name).get("content", None)

    def update_commands(self):
        # update all command sets
        for cs in self.command_sets:
            if cs.leave_alone:
                if not cs.name == EMBEDDED_COMMAND_SET_NAME:
                    logger.warning(f"Will not update the command set '{cs.name}', it wants to be left alone.")
                continue
            # update command set
            logger.info(f"Updating the command set '{cs.name}'...")
            cs.update()
            logger.info(f"Command set '{cs.name}' updated!")

    def _configure(self, readonly: bool = False):
        # make sure a profile exists and is loaded
        new_profile: Optional[str] = None
        if self._profile is None:
            if readonly:
                raise ConfigNotPresent()
            print()
            print("You need to choose the distribution you want to work with.")
            distros: List[Choice] = []
            for distro in KNOWN_DISTRIBUTIONS.values():
                if distro.staging:
                    continue
                eol: str = "" if distro.end_of_life is None else f"(end of life: {distro.end_of_life_fmt})"
                label = [("class:choice", distro.name), ("class:disabled", f"  {eol}")]
                choice: Choice = Choice(title=label, value=distro.name)
                if distro.name == SUGGESTED_DISTRIBUTION:
                    distros.insert(0, choice)
                else:
                    distros.append(choice)
            # let the user choose the distro
            new_profile = questionary.select(
                "Choose a distribution:", choices=distros, style=cli_style).unsafe_ask()

        # make a new profile if needed
        if new_profile is not None:
            if readonly:
                raise ConfigNotPresent()
            print(f"Setting up a new shell profile '{new_profile}'...")
            self._profile = ShellProfile(name=new_profile)
            # set the new profile as the profile to load at the next launch
            self.settings.profile = new_profile
            # configure profile
            self._profile.configure()

    def _show_banner(self, profile: Optional[ShellProfile], billboard: Optional[str]):
        width: int = 120
        if profile is None:
            # first launch -> bigger banner
            txt_width: int = 95  # measured
            padding: int = int(floor((width - txt_width) / 2))
            fmt: Figlet = Figlet(font='standard', width=width, justify='center')
            fig: str = fmt.renderText(DNAME.replace(" ", "   ").upper()).rstrip()
            sep: str = text_justify(termcolor.colored("_" * txt_width, "yellow", attrs=["bold"]), width)
            extras: str = text_distribute([
                f"First Setup - Welcome!",
                f"v{__version__}"
            ], width=txt_width)
            txt: str = f"{fig}\n{sep}\n{' ' * padding}{extras}\n\n"
        else:
            # any other launch -> smaller banner
            txt_width: int = 81  # measured
            padding: int = int(floor((width - txt_width) / 2))
            fmt: Figlet = Figlet(font='small', width=width, justify='center')
            fig: str = fmt.renderText(DNAME.replace(" ", "   ").upper()).rstrip()
            sep: str = text_justify(termcolor.colored("_" * txt_width, "yellow", attrs=["bold"]), width)
            extras: str = text_distribute([
                f"Profile: {self.settings.profile}",
                f"v{__version__}"
            ], width=txt_width)
            txt: str = f"{fig}\n{sep}\n{' ' * padding}{extras}\n\n"
        # ---
        print(txt.rstrip())
        print()
        print("+" + "-" * (width - 2) + "+")
        # print billboard (if given)
        if billboard:
            print("💬", billboard.strip())
            print("+" + "-" * (width - 2) + "+")

    def _check_shell_token(self, fix_if_needed: bool = True):
        profile: ShellProfile = self.profile
        if profile is None:
            return
        # we have a profile, check if we have a token
        db: DTShellDatabase = profile.database("shell-token")
        token_str: str = db.get("token", None)
        if token_str is None:
            return
        # parse token
        try:
            token: Optional[DuckietownToken] = DuckietownToken.from_string(token_str)
        except:
            logger.debug(traceback.format_exc())
            # drop token, the background task will create a new one later
            if token_str is not None:
                db.delete("token")
            return
        # check scope
        if not token.grants(action="run", resource="shell"):
            if fix_if_needed and profile.needs_update("shell-token", CHECK_SHELL_TOKEN_SECS):
                from .tasks import ShellTokenTask
                logger.info("Checking permissions...")
                try:
                    ShellTokenTask(self).execute()
                    self._check_shell_token(fix_if_needed=False)
                except:
                    return
            raise UserError("Your account does not seem to have access to the capability needed to run the "
                            "Duckietown shell. If you believe this is a mistake, please, reach out to "
                            "the Duckietown technical support via Slack or via "
                            "https://duckietown.com/contact/.")
