from dt_shell import DTCommandAbs


__all__ = ["DTCommand"]

from dt_shell import DTShell


class DTCommand(DTCommandAbs):

    @staticmethod
    def command(shell: DTShell, args):
        shell.update_commands()
