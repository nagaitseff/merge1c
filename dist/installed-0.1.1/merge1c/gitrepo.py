"""Small, byte-preserving adapter around the Git 2.50+ command line.

No command moves an existing branch, updates the user's index, or changes Git
configuration.  The only publication operation creates a new candidate branch.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import tempfile
from typing import Any

from .processes import find_program


class GitError(RuntimeError):
    """Git failed, or an operation would violate the adapter's invariants."""


_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REPOSITORY_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX",
)


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _quote_path(value: str) -> bytes:
    """Git's stdin-paths accepts C-quoted UTF-8, including octal bytes."""
    output = bytearray(b'"')
    for byte in value.encode("utf-8", errors="surrogateescape"):
        if byte in (34, 92):
            output.extend(b"\\" + bytes([byte]))
        elif 32 <= byte < 127:
            output.append(byte)
        else:
            output.extend(("\\%03o" % byte).encode("ascii"))
    output.extend(b'"\n')
    return bytes(output)


def _relative_path(name: str) -> Path:
    """Reject Git paths that could escape or alias a destination on Windows."""
    parts = name.split("/")
    if (
        not name or "\\" in name or PurePosixPath(name).is_absolute()
        or PureWindowsPath(name).drive
        or any(part in ("", ".", "..") or part.casefold() == ".git" for part in parts)
    ):
        raise GitError("Unsafe tree path: %r" % name)
    if os.name == "nt":
        reserved = {"CON", "PRN", "AUX", "NUL"}
        reserved.update("COM%d" % number for number in range(1, 10))
        reserved.update("LPT%d" % number for number in range(1, 10))
        if any(
            ":" in part or part.endswith((".", " "))
            or part.split(".", 1)[0].upper() in reserved for part in parts
        ):
            raise GitError("Unsupported Windows tree path: %r" % name)
    return Path(*parts)


def _is_link(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    # pathlib.is_junction was added in 3.12; keep the 3.11 contract too.
    try:
        return getattr(path.lstat(), "st_reparse_tag", None) == 0xA0000003
    except FileNotFoundError:
        return False


def _check_link_ancestors(path: Path) -> None:
    for item in (path, *path.parents):
        if _is_link(item):
            raise GitError("Symbolic links/junctions are unsupported in destination paths: %s" % item)


class GitRepository:
    def __init__(
        self, path: str | os.PathLike[str], executable: str = "git",
        *, minimum_version: str = "2.50.0",
    ) -> None:
        self.path = Path(path).resolve()
        try:
            self.executable = find_program(os.fspath(executable))
        except ValueError as exc:
            raise GitError(str(exc)) from exc
        if self.executable is None:
            raise GitError("Git executable was not found: %s" % executable)
        required = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", minimum_version) if isinstance(minimum_version, str) else None
        if required is None:
            raise GitError("minimum_version must have the form 2.50 or 2.50.0")
        required_parts = tuple(int(item or 0) for item in required.groups())
        if required_parts < (2, 50, 0):
            raise GitError("The minimum supported Git version cannot be lower than 2.50.0")
        self.minimum_version = minimum_version
        result = self._run(["--version"])
        self.version = _decode(result.stdout).strip().removeprefix("git version ")
        version = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", self.version)
        if version is None or tuple(int(item or 0) for item in version.groups()) < required_parts:
            raise GitError("Git %s or newer is required; found %s" % (minimum_version, self.version))
        bare = self._run(["rev-parse", "--is-bare-repository"]).stdout.strip()
        if bare == b"true":
            raise GitError("A Git working-tree repository is required")
        self.path = Path(_decode(self._run(["rev-parse", "--show-toplevel"]).stdout).rstrip("\r\n")).resolve()

    def _environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        for name in _REPOSITORY_ENV:
            env.pop(name, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        if extra:
            env.update(extra)
        return env

    def _command(self, arguments: list[str]) -> list[str]:
        return [
            self.executable, "-C", str(self.path),
            "-c", "rerere.enabled=false", "-c", "rerere.autoupdate=false",
            *arguments,
        ]

    def _run(
        self, arguments: list[str], *, input: bytes | None = None,
        allowed: tuple[int, ...] = (0,), env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                self._command(arguments), input=input, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self._environment(env), check=False,
            )
        except (OSError, ValueError) as exc:
            raise GitError("Cannot execute Git: %s" % exc) from exc
        if result.returncode not in allowed:
            detail = _decode(result.stderr).strip() or _decode(result.stdout).strip()
            raise GitError("git %s failed (%d): %s" % (arguments[0], result.returncode, detail))
        return result

    def resolve(self, ref: str) -> str:
        oid = _decode(self._run([
            "rev-parse", "--verify", "--end-of-options", ref + "^{commit}",
        ]).stdout).strip()
        if not _OID.fullmatch(oid):
            raise GitError("Git returned an invalid commit object name")
        return oid

    def head(self) -> str:
        return self.resolve("HEAD")

    def status(self) -> str:
        """Return NUL-delimited porcelain status for tracked files only."""
        return _decode(self._run([
            "status", "--porcelain=v1", "-z", "--untracked-files=no",
        ]).stdout)

    def operation_in_progress(self) -> bool:
        return self._active_operation() is not None

    def _active_operation(self) -> str | None:
        names = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply")
        paths = self._run(["rev-parse", "--path-format=absolute", "--git-path", names[0]]).stdout
        # Each linked worktree has its own operation metadata, resolved by Git.
        git_directory = Path(_decode(paths).strip()).parent
        for name in names:
            if (git_directory / name).exists():
                return name
        return None

    def merge_bases(self, local: str, incoming: str) -> list[str]:
        left, right = self.resolve(local), self.resolve(incoming)
        result = self._run(["merge-base", "--all", left, right], allowed=(0, 1))
        return [_decode(item) for item in result.stdout.splitlines()]

    def changed_paths(self, base: str, tip: str) -> list[str]:
        left, right = self.resolve(base), self.resolve(tip)
        result = self._run([
            "diff", "--name-only", "-z", "--no-renames", left, right, "--",
        ])
        return [_decode(item) for item in result.stdout.split(b"\0") if item]

    def _tree_entries(self, revision: str) -> list[tuple[str, str, str]]:
        result = self._run(["ls-tree", "-r", "-z", "--full-tree", revision])
        entries = []
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            name = _decode(raw_path)
            _relative_path(name)
            if kind != "blob" or mode not in ("100644", "100755"):
                raise GitError("Unsupported symbolic link or submodule: %s" % name)
            entries.append((mode, oid, name))
        return entries

    def export_tree(self, revision: str, destination: str | os.PathLike[str]) -> None:
        """Stream raw blobs, deliberately bypassing archive attributes/filters.

        Export-ignore and export-subst must never omit or alter source files.
        Symbolic links and gitlinks are explicitly unsupported in this MVP.
        """
        commit = self.resolve(revision)
        entries = self._tree_entries(commit)
        requested = Path(destination)
        target = requested.absolute()
        _check_link_ancestors(target)
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise GitError("Export destination must be absent or an empty directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".merge1c-export-", dir=target.parent))
        process = None
        try:
            with tempfile.TemporaryFile() as errors:
                try:
                    process = subprocess.Popen(
                        self._command(["cat-file", "--batch"]), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=errors, env=self._environment(),
                    )
                except OSError as exc:
                    raise GitError("Cannot execute Git: %s" % exc) from exc
                assert process.stdin is not None and process.stdout is not None
                for mode, oid, name in entries:
                    process.stdin.write(oid.encode("ascii") + b"\n")
                    process.stdin.flush()
                    header = process.stdout.readline().split()
                    if len(header) != 3 or header[:2] != [oid.encode("ascii"), b"blob"]:
                        raise GitError("Invalid Git blob response for %s" % name)
                    remaining = int(header[2])
                    output = stage / _relative_path(name)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with output.open("xb") as stream:
                        while remaining:
                            chunk = process.stdout.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise GitError("Truncated Git blob: %s" % name)
                            stream.write(chunk)
                            remaining -= len(chunk)
                    if process.stdout.read(1) != b"\n":
                        raise GitError("Malformed Git blob terminator: %s" % name)
                    if os.name != "nt":
                        output.chmod(0o755 if mode == "100755" else 0o644)
                process.stdin.close()
                process.stdout.close()
                returncode = process.wait()
                if returncode:
                    errors.seek(0)
                    raise GitError("git cat-file failed (%d): %s" % (returncode, _decode(errors.read())))
            # Recheck so that concurrent destination edits cannot be overwritten.
            _check_link_ancestors(target)
            if target.exists():
                if not target.is_dir() or any(target.iterdir()):
                    raise GitError("Export destination changed during export")
                target.rmdir()
            stage.rename(target)
        except (OSError, ValueError) as exc:
            raise GitError("Cannot export tree: %s" % exc) from exc
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                if process.stdout is not None and not process.stdout.closed:
                    process.stdout.close()
            if stage.exists():
                shutil.rmtree(stage)

    def preview(self, local: str, incoming: str) -> dict[str, Any]:
        left, right = self.resolve(local), self.resolve(incoming)
        result = self._run([
            "merge-tree", "--write-tree", "-z", "-Xno-renames", left, right,
        ], allowed=(0, 1))
        chunks = result.stdout.split(b"\0")
        tree = _decode(chunks[0])
        if not _OID.fullmatch(tree):
            raise GitError("Git merge-tree did not return a tree object")
        conflicts: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        index = 1
        if result.returncode == 1:
            try:
                while index < len(chunks) and chunks[index]:
                    metadata, path = chunks[index].split(b"\t", 1)
                    mode, oid, stage = metadata.decode("ascii").split()
                    conflicts.append({"path": _decode(path), "mode": mode, "oid": oid, "stage": int(stage)})
                    index += 1
                index += 1
                while index < len(chunks) and chunks[index]:
                    path_count = int(chunks[index])
                    index += 1
                    paths = [_decode(item) for item in chunks[index:index + path_count]]
                    index += path_count
                    kind, message = chunks[index:index + 2]
                    index += 2
                    messages.append({"paths": paths, "type": _decode(kind), "message": _decode(message)})
            except (ValueError, IndexError) as exc:
                raise GitError("Invalid Git merge-tree conflict protocol") from exc
        return {
            "tree": tree, "clean": result.returncode == 0, "conflicts": conflicts,
            "messages": messages, "local": left, "incoming": right,
        }

    def _check_candidate_source(self, local: str) -> None:
        if self.head() != local:
            raise GitError("HEAD changed since the local input was captured; prepare a new session")
        operation = self._active_operation()
        if operation:
            raise GitError("Cannot publish a candidate during an active Git operation: %s" % operation)

    def _service_identity_fallback(self) -> dict[str, str]:
        """Provide missing identity values without writing user configuration."""
        result: dict[str, str] = {}
        for key, suffix, fallback in (
            ("user.name", "NAME", "merge1c"),
            ("user.email", "EMAIL", "merge1c@localhost.invalid"),
        ):
            value = self._run(["config", "--get", key], allowed=(0, 1)).stdout.strip()
            if not value:
                for role in ("AUTHOR", "COMMITTER"):
                    variable = "GIT_%s_%s" % (role, suffix)
                    if not os.environ.get(variable):
                        result[variable] = fallback
        return result

    @staticmethod
    def _session_ref(session_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise GitError("Session ID must be 32 lowercase hexadecimal UUID characters")
        return "refs/merge1c/sessions/" + session_id

    def pin_session(
        self, local_oid: str, incoming_oid: str, session_id: str, payload: bytes,
    ) -> dict[str, str]:
        """Keep immutable input commits and a manifest reachable through Git GC.

        The input commits are real parents; object names embedded in JSON alone
        would not preserve reachability. The ref must not already exist.
        """
        refname = self._session_ref(session_id)
        local, incoming = self.resolve(local_oid), self.resolve(incoming_oid)
        if not isinstance(payload, bytes):
            raise GitError("Session manifest payload must be bytes")
        blob = _decode(self._run(
            ["hash-object", "-w", "--no-filters", "--stdin"], input=payload,
        ).stdout).strip()
        tree_input = ("100644 blob %s\tsession.json\0" % blob).encode("ascii")
        tree = _decode(self._run(["mktree", "-z"], input=tree_input).stdout).strip()
        arguments = ["commit-tree", tree, "-p", local]
        if incoming != local:
            arguments += ["-p", incoming]
        commit = _decode(self._run(
            arguments, input=("merge1c durable session %s\n" % session_id).encode("ascii"),
            env=self._service_identity_fallback(),
        ).stdout).strip()
        transaction = "start\ncreate %s %s\nprepare\ncommit\n" % (refname, commit)
        self._run(["update-ref", "--stdin", "-m", "merge1c: pin immutable session inputs"], input=transaction.encode("ascii"))
        return {"ref": refname, "commit": commit}

    def release_session(self, ref: str, expected_oid: str) -> None:
        """Delete only an owned session ref, conditional on its recorded value."""
        prefix = "refs/merge1c/sessions/"
        if not ref.startswith(prefix) or ref != self._session_ref(ref[len(prefix):]):
            raise GitError("Only merge1c session references may be released")
        if not _OID.fullmatch(expected_oid):
            raise GitError("An exact expected session commit OID is required")
        self._run(["update-ref", "-m", "merge1c: release session pin", "-d", ref, expected_oid])

    def _candidate_entries(self, directory: Path) -> list[tuple[str, Path, os.stat_result]]:
        entries: list[tuple[str, Path, os.stat_result]] = []
        for current, folders, filenames in os.walk(directory, followlinks=False):
            for name in folders + filenames:
                item = Path(current) / name
                if _is_link(item):
                    raise GitError("Candidate symbolic links/junctions are unsupported: %s" % item)
                _relative_path(item.relative_to(directory).as_posix())
            for name in filenames:
                item = Path(current) / name
                if not item.is_file():
                    raise GitError("Candidate contains a non-regular file: %s" % item)
                entries.append((item.relative_to(directory).as_posix(), item, item.stat()))
        entries.sort(key=lambda entry: entry[0].encode("utf-8", errors="surrogateescape"))
        return entries

    def _verify_captured_blobs(self, objects: list[tuple[str, bytes]], expected: dict[str, str]) -> None:
        """Check immutable captured blobs, rather than rereading mutable files."""
        if not objects:
            return
        with tempfile.TemporaryFile() as errors:
            try:
                process = subprocess.Popen(
                    self._command(["cat-file", "--batch"]), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=errors, env=self._environment(),
                )
            except OSError as exc:
                raise GitError("Cannot execute Git: %s" % exc) from exc
            assert process.stdin is not None and process.stdout is not None
            try:
                for name, oid in objects:
                    process.stdin.write(oid + b"\n")
                    process.stdin.flush()
                    header = process.stdout.readline().split()
                    if len(header) != 3 or header[:2] != [oid, b"blob"]:
                        raise GitError("Invalid captured blob response: %s" % name)
                    remaining = int(header[2])
                    digest = sha256()
                    while remaining:
                        block = process.stdout.read(min(1024 * 1024, remaining))
                        if not block:
                            raise GitError("Truncated captured blob: %s" % name)
                        digest.update(block)
                        remaining -= len(block)
                    if process.stdout.read(1) != b"\n":
                        raise GitError("Malformed captured blob: %s" % name)
                    if digest.hexdigest() != expected[name]:
                        raise GitError("Candidate differs from the checked snapshot: %s" % name)
                process.stdin.close()
                process.stdout.close()
                if process.wait():
                    errors.seek(0)
                    raise GitError("git cat-file verification failed: %s" % _decode(errors.read()))
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if not process.stdin.closed:
                    process.stdin.close()
                if not process.stdout.closed:
                    process.stdout.close()

    def create_candidate(
        self, candidate_directory: str | os.PathLike[str], local_oid: str,
        incoming_oid: str, branch_name: str, message: str,
        *, expected_hashes: dict[str, str] | None = None,
    ) -> str:
        """Create an exact-byte merge commit on a NEW branch; never check it out.

        POSIX executable bits come from the candidate files. On Windows, where
        those bits are unavailable, preserve known local modes (incoming modes
        for newly introduced paths); completely new files use mode 100644.

        Session callers supply expected_hashes from their successful checks.
        The complete path set and captured immutable blobs must match it before
        any new reference can be published.
        """
        local, incoming = self.resolve(local_oid), self.resolve(incoming_oid)
        if local == incoming:
            raise GitError("Local and incoming inputs are the same commit; no two-parent merge is needed")
        self._check_candidate_source(local)
        short_name = branch_name.removeprefix("refs/heads/")
        if branch_name.startswith("refs/") and not branch_name.startswith("refs/heads/"):
            raise GitError("Candidate must be a new branch under refs/heads/")
        self._run(["check-ref-format", "--branch", short_name])
        refname = "refs/heads/" + short_name
        exists = self._run(["show-ref", "--verify", "--quiet", refname], allowed=(0, 1))
        if exists.returncode == 0:
            raise GitError("Candidate branch already exists: %s" % short_name)
        directory = Path(candidate_directory)
        _check_link_ancestors(directory.absolute())
        if not directory.is_dir():
            raise GitError("Candidate directory must be a real directory")
        directory = directory.resolve()
        entries = self._candidate_entries(directory)
        if expected_hashes is not None:
            expected_hashes = dict(expected_hashes)
            if set(expected_hashes) != {name for name, _, _ in entries}:
                raise GitError("Candidate file set differs from the checked snapshot")
            if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in expected_hashes.values()):
                raise GitError("Expected candidate hashes must be exact SHA-256 values")
        modes = {name: mode for mode, _, name in self._tree_entries(incoming)}
        modes.update({name: mode for mode, _, name in self._tree_entries(local)})
        if entries:
            hashes = self._run(
                ["hash-object", "-w", "--no-filters", "--stdin-paths"],
                input=b"".join(_quote_path(item.as_posix()) for _, item, _ in entries),
            ).stdout.splitlines()
        else:
            hashes = []
        if len(hashes) != len(entries) or any(not _OID.fullmatch(_decode(oid)) for oid in hashes):
            raise GitError("Git returned an invalid candidate blob list")
        index_input = bytearray()
        for (name, item, before), oid in zip(entries, hashes, strict=True):
            after = item.stat()
            if _is_link(item) or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise GitError("Candidate file changed while being captured: %s" % name)
            if os.name == "nt":
                mode = modes.get(name, "100644")
            else:
                mode = "100755" if after.st_mode & 0o111 else "100644"
            index_input.extend(mode.encode("ascii") + b" " + oid + b"\t" + name.encode("utf-8", errors="surrogateescape") + b"\0")
        if expected_hashes is not None:
            self._verify_captured_blobs([(entry[0], oid) for entry, oid in zip(entries, hashes, strict=True)], expected_hashes)
        with tempfile.TemporaryDirectory(prefix="merge1c-index-") as temporary:
            env = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self._run(["read-tree", "--empty"], env=env)
            if index_input:
                self._run(["update-index", "-z", "--index-info"], input=bytes(index_input), env=env)
            tree = _decode(self._run(["write-tree"], env=env).stdout).strip()
        commit = _decode(self._run(
            ["commit-tree", tree, "-p", local, "-p", incoming],
            input=message.encode("utf-8"),
        ).stdout).strip()
        self._check_candidate_source(local)
        latest = self._candidate_entries(directory)
        def signature(found):
            return [(name, info.st_size, info.st_mtime_ns, info.st_ino) for name, _, info in found]
        if signature(latest) != signature(entries):
            raise GitError("Candidate changed before publication; run checks again")
        transaction = "start\nverify HEAD %s\ncreate %s %s\nprepare\ncommit\n" % (local, refname, commit)
        self._run(["update-ref", "--stdin", "-m", "merge1c: create reviewed candidate"], input=transaction.encode("utf-8"))
        return commit
