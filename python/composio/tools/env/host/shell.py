"""Host shell session wrapper."""

import os
import re
import select
import subprocess
import time
import typing as t
from abc import abstractmethod
from pathlib import Path

import paramiko

from composio.tools.env.base import Sessionable
from composio.tools.env.constants import ECHO_EXIT_CODE, EXIT_CODE, STDERR, STDOUT
from composio.tools.env.id import generate_id


_ANSI_ESCAPE = re.compile(
    rb"""
    \x1B
    (?:
        [@-Z\\-_]
    |
        \[
        [0-?]*
        [ -/]*
        [@-~]
    )
""",
    re.VERBOSE,
)


_DEV_SOURCE = Path("/home/user/.dev/bin/activate")
_NOWAIT_CMDS = ("cd", "ls", "pwd")

# Non-exhaustive list of interactive commands
_INTERACTIVE_COMMANDS = ("tail -f", "watch", "top", "htop", "less", "more", "vim", "nano", "vi")


class Shell(Sessionable):
    """Abstract shell session."""

    def sanitize_command(self, cmd: str) -> bytes:
        """Prepare command string."""
        return (cmd.rstrip() + "\n").encode()

    @abstractmethod
    def exec(self, cmd: str, wait: bool = True) -> t.Dict:
        """Execute command on container."""


# TODO: Execute in a virtual environment
class HostShell(Shell):
    """Host interactive shell."""

    _process: subprocess.Popen

    def __init__(self, environment: t.Optional[t.Dict] = None) -> None:
        """Initialize shell."""
        super().__init__()
        self._id = generate_id()
        self.environment = environment or {}

    def setup(self) -> None:
        """Setup host shell."""
        self.logger.debug(f"Setting up shell: {self.id}")
        self._process = subprocess.Popen(  # pylint: disable=consider-using-with
            args=["/bin/bash", "-l", "-m"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self.environment,
        )
        self.logger.debug(
            "Initial data from session: %s - %s",
            self.id,
            self._read(wait=False),
        )

        # Load development environment if available
        if _DEV_SOURCE.exists():
            self.logger.debug("Loading development environment")
            self.exec(f"source {_DEV_SOURCE}")

        # Setup environment
        for key, value in self.environment.items():
            self.exec(f"export {key}={value}")
            time.sleep(0.05)

    def _is_interactive_command(self, cmd: str) -> bool:
        """Check if command is interactive."""
        # Split on both ; and && to handle composite commands
        for subcmd in cmd.replace("&&", ";").split(";"):
            subcmd = subcmd.lower().strip()
            cmd_parts = subcmd.split()
            if not cmd_parts:
                continue

            # Check if any part of the composite command is interactive
            if any(
                cmd_parts[0] == interactive_cmd.split()[0] and subcmd.startswith(interactive_cmd)
                for interactive_cmd in _INTERACTIVE_COMMANDS
            ):
                return True

        return False


    def _has_command_exited(self, cmd: str) -> bool:
        """Waif for command to exit."""
        commands = [cmd.strip() for cmd in cmd.split("&&")]

        # Check if all commands are no-wait commands
        all_nowait = all(
            cmd.split()[0] in _NOWAIT_CMDS
            for cmd in commands
        )
        
        if all_nowait:
            time.sleep(0.3)
            return True
        output = subprocess.run(  # pylint: disable=subprocess-run-check
            ["ps", "-e", "-o", "args="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.decode()
        # Check each process against our commands
        for process in output.splitlines()[1:]:  # Skip header row
            process = process.strip()
            # Check if any of our commands exactly match the process
            if any(
                # Either exact match or command with arguments
                process == command or process.startswith(f"{command} ")
                for command in commands
            ):
                return False
        return True
    
    def _read(
        self,
        cmd: t.Optional[str] = None,
        command_marker: t.Optional[str] = None,
        stderr_marker: t.Optional[str] = None,
        wait: bool = True,
        timeout: float = 120.0,
    ) -> t.Dict:
        """Read data from a subprocess with a timeout."""
        stderr = t.cast(t.IO[str], self._process.stderr).fileno()
        stdout = t.cast(t.IO[str], self._process.stdout).fileno()
        buffer = {stderr: b"", stdout: b""}
        if wait and cmd is None:
            raise ValueError("`cmd` cannot be `None` when `wait` is set to `True`")

        end_time = time.time() + timeout
        stdout_data = None
        stderr_data = None
        markers_status = {
            stdout: False,
            stderr: False,
        }
        while time.time() < end_time:
            if wait and cmd and not self._has_command_exited(cmd=str(cmd)):
                time.sleep(0.5)
                continue

            readables, _, _ = select.select([stderr, stdout], [], [], 0.1)
            if readables:
                for fd in readables:
                    if markers_status[fd]:
                        continue
                    data = os.read(fd, 4096)
                    if data:
                        buffer[fd] += data

                # Check if we've seen our specific marker

                # Check if we've seen both markers
                stdout_data = buffer[stdout].decode()
                stderr_data = buffer[stderr].decode()

                markers_found = True
                if command_marker is not None and command_marker not in stdout_data:
                    markers_found = False
                else:
                    markers_status[stdout] = True
                if stderr_marker is not None and stderr_marker not in stderr_data:
                    markers_found = False
                else:
                    markers_status[stderr] = True
                if markers_found:
                    # Remove the markers from output
                    if command_marker:
                        stdout_data = stdout_data[:stdout_data.find(command_marker)]
                    if stderr_marker:
                        stderr_data = stderr_data[:stderr_data.find(stderr_marker)]
                    break
            if cmd is None or cmd == "":
                break
            time.sleep(0.05)

        if self._process.poll() is not None:
            raise RuntimeError(
                f"Subprocess exited unexpectedly.\nCurrent buffer: {buffer}"
            )

        if time.time() >= end_time:
            raise TimeoutError(
                "Timeout reached while reading from subprocess.\nCurrent "
                f"buffer: {buffer}. Note that interactive commands are not supported "
                "and can timeout. Use a corresponding non-interactive command if possible."
            )

        return {
            STDOUT: stdout_data if stdout_data is not None else buffer[stdout].decode(),
            STDERR: stderr_data if stderr_data is not None else buffer[stderr].decode(),
        }

    def _write(self, cmd: str) -> None:
        """Write command to shell."""
        try:
            stdin = t.cast(t.IO[str], self._process.stdin)
            os.write(stdin.fileno(), self.sanitize_command(cmd=cmd))
            stdin.flush()
        except BrokenPipeError as e:
            raise RuntimeError(str(e)) from e

    def exec(self, cmd: str, wait: bool = True) -> t.Dict:  # type: ignore
        """Execute command on container."""

        if self._is_interactive_command(cmd):
            raise ValueError(
                "Interactive commands are not supported."
                f" Command '{cmd}' appears to be interactive."
            )

        # Add a unique marker specific to this command, but capture exit code first
        cmd_hash = hash(cmd)  # Use hash of command to make marker unique
        cmd_end_prefix = f"__CMD_END"
        command_marker = f"{cmd_end_prefix}_{self._id}_{cmd_hash}__"
        exit_prefix = f"__EXIT"
        exit_marker = f"{exit_prefix}_{cmd_hash}__"
        stderr_end_prefix = f"__STDERR_END"
        stderr_marker = f"{stderr_end_prefix}_{self._id}_{cmd_hash}__"

        # Command to add end markers for both stdout and stderr and capture exit code
        # Write stdout marker to stdout, stderr marker directly to /dev/stderr
        # Capture exit code of the command, then add our markers
        # Use 'true' command if cmd is empty to avoid syntax error with leading semicolon
        safe_cmd = cmd if cmd else "true"
        marked_cmd = (
            f"{safe_cmd}; "
            f"echo '{exit_marker} '$?; "  # Capture exit code immediately after command
            f"echo '{command_marker}'; "
            f"printf '{stderr_marker}' > /dev/stderr"
        )

        # Write the command
        self._write(cmd=marked_cmd)
        # Read until we see the command marker
        result = self._read(cmd=safe_cmd, command_marker=command_marker, stderr_marker=stderr_marker, wait=wait)

        # Extract exit code from the output
        stdout = result[STDOUT]
        exit_code = 0

        # Look for our exit marker in the output
        if exit_marker in stdout:
            new_stdout_lines = []
            try:
                # Process all lines in a single loop
                for line in stdout.splitlines():
                    if exit_marker in line:
                        exit_code = int(line.split(exit_marker)[1].strip())
                        new_stdout_lines.append(line.split(exit_marker)[0])
                    else:
                        new_stdout_lines.append(line)
            except ValueError:
                exit_code = 1

            stdout = '\n'.join(new_stdout_lines)

        # if previous command timed out, its outputs may be mixed with the current command stdout
        if exit_prefix in stdout:
            stdout = stdout[:stdout.find(exit_prefix)]

        return {
            STDOUT: stdout,
            STDERR: result[STDERR],
            EXIT_CODE: exit_code
        }

    def teardown(self) -> None:
        """Stop and remove the running shell."""
        self._process.kill()


class SSHShell(Shell):
    """Interactive shell over SSH session."""

    def __init__(
        self, client: paramiko.SSHClient, environment: t.Optional[t.Dict] = None
    ) -> None:
        """Initialize interactive shell."""
        super().__init__()
        self._id = generate_id()
        self.client = client
        self.environment = environment or {}
        self.channel = self.client.invoke_shell(environment=self.environment)

    def setup(self) -> None:
        """Invoke shell."""
        self.logger.debug(f"Setting up shell: {self.id}")

        # Load development environment if available
        if _DEV_SOURCE.exists():
            self.logger.debug("Loading development environment")
            self.exec(f"source {_DEV_SOURCE}")

        # Setup environment
        for key, value in self.environment.items():
            self._send(f"export {key}={value}")
            time.sleep(0.05)
            self._read()

        # CD to user dir
        self.exec(cmd="cd ~/ && export PS1=''")

    def _send(self, buffer: str, stdin: t.Optional[str] = None) -> None:
        """Send buffer to shell."""
        if stdin is None:
            self.channel.sendall(f"{buffer}\n".encode("utf-8"))
            time.sleep(0.05)
            return

        self.channel.send(f"{buffer}\n".encode("utf-8"))
        self.channel.sendall(f"{stdin}\n".encode("utf-8"))
        time.sleep(0.05)

    def _read(self) -> str:
        """Read buffer from shell."""
        output = b""
        while self.channel.recv_ready():
            output += self.channel.recv(512)
        while self.channel.recv_stderr_ready():
            output += self.channel.recv_stderr(512)
        return _ANSI_ESCAPE.sub(b"", output).decode(encoding="utf-8")

    def _wait(self, cmd: str) -> None:
        """Wait for the command to execute."""
        _cmd, *_rest = cmd.split(" ")
        if _cmd in _NOWAIT_CMDS or len(_rest) == 0:
            time.sleep(0.3)
            return

        while True:
            _, stdout, _ = self.client.exec_command(command="ps -eo command")
            if all(
                not line.lstrip().rstrip().endswith(cmd)
                for line in stdout.read().decode().split("\n")
            ):
                return
            time.sleep(0.3)

    def _exit_status(self) -> int:
        """Wait for the command to execute."""
        self._send(buffer="echo $?")
        try:
            output = self._read().split("\n")
            if len(output) == 1:
                return int(output[0].lstrip().rstrip())
            return int(output[1].lstrip().rstrip())
        except ValueError:
            return 1

    def _sanitize_output(self, output: str) -> str:
        """Clean the output."""
        lines = list(map(lambda x: x.rstrip(), output.split("\r\n")))
        clean = "\n".join(lines[1:])
        if clean.startswith("\r"):
            clean = clean[1:]
        return clean.replace("(.dev)\n", "")

    def exec(self, cmd: str, wait: bool = True) -> t.Dict:
        """Execute a command and return output and exit code."""
        output = ""
        for _cmd in cmd.split(" && "):
            self._send(buffer=_cmd)
            if wait:
                self._wait(cmd=_cmd)
            output += self._sanitize_output(output=self._read())

        return {
            STDOUT: output,
            STDERR: "",
            EXIT_CODE: str(self._exit_status()),
        }

    def teardown(self) -> None:
        """Close the SSH channel."""
        self.channel.close()
