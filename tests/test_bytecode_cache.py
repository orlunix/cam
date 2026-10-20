"""Standalone cache validity, ownership and transparent script behavior."""

import ast
import builtins
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from camc_pkg.prelude import bytecode_bootstrap as bootstrap

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def program(tmp_path):
    source = tmp_path / "script with spaces"
    source.write_text("result = 17\n")
    return source, tmp_path / "cache"


def execute(source, cache):
    namespace = {}
    exec(bootstrap._camc_cached_code(str(source), str(cache)), namespace)
    return namespace["result"]


def test_repeat_launch_uses_cached_code(program, monkeypatch):
    source, cache = program
    assert execute(source, cache) == 17
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert stat.S_IMODE(next(cache.glob("*.pyc")).stat().st_mode) == 0o600
    def unexpected_compile(*_a, **_kw):
        raise AssertionError("warm cache recompiled source")
    monkeypatch.setattr(builtins, "compile", unexpected_compile)
    assert execute(source, cache) == 17


def test_same_size_edit_invalidates_even_with_preserved_timestamp(program):
    source, cache = program
    assert execute(source, cache) == 17
    info = source.stat()
    source.write_text("result = 23\n")
    os.utime(str(source), ns=(info.st_atime_ns, info.st_mtime_ns))
    assert execute(source, cache) == 23
    assert len(list(cache.glob("*.pyc"))) == 2


def test_python_version_invalidates(program, monkeypatch):
    source, cache = program
    execute(source, cache)
    monkeypatch.setattr(bootstrap.sys, "version", sys.version + "-different-runtime")
    assert execute(source, cache) == 17
    assert len(list(cache.glob("*.pyc"))) == 2


@pytest.mark.parametrize("contents", [b"", b"broken", b"\x00" * 128])
def test_corrupt_cache_is_rebuilt(program, contents):
    source, cache = program
    execute(source, cache)
    path = next(cache.glob("*.pyc"))
    path.write_bytes(contents)
    assert execute(source, cache) == 17
    assert path.stat().st_size > len(contents)


def test_non_code_marshaled_payload_is_rejected(program):
    source, cache = program
    execute(source, cache)
    path = next(cache.glob("*.pyc"))
    header_size = 16 if sys.version_info >= (3, 7) else 12
    header = path.read_bytes()[:header_size]
    encoded = bootstrap.marshal.dumps("not executable code")
    checksum = bootstrap.hashlib.sha256(path.stem.encode("ascii") + header + encoded).digest()
    path.write_bytes(header + encoded + checksum)
    assert execute(source, cache) == 17


def test_older_code_renamed_to_current_key_is_rejected(program):
    source, cache = program
    execute(source, cache)
    old = next(cache.glob("*.pyc"))
    old_bytes = old.read_bytes()
    source.write_text("result = 23\n")
    execute(source, cache)
    new = next(path for path in cache.glob("*.pyc") if path != old)
    new.write_bytes(old_bytes)
    assert execute(source, cache) == 23


def test_unsafe_directory_falls_back_without_writing(program):
    source, cache = program
    cache.mkdir(mode=0o755)
    assert execute(source, cache) == 17
    assert not list(cache.iterdir())


def test_symlink_directory_falls_back(program, tmp_path):
    source, cache = program
    target = tmp_path / "foreign"
    target.mkdir(mode=0o700)
    cache.symlink_to(target, target_is_directory=True)
    assert execute(source, cache) == 17
    assert not list(target.iterdir())


def test_foreign_directory_falls_back(program, monkeypatch):
    source, cache = program
    cache.mkdir(mode=0o700)
    real_lstat = bootstrap.os.lstat
    def lstat(path):
        info = real_lstat(path)
        fields = list(info)
        fields[4] = os.getuid() + 1
        return os.stat_result(fields)
    monkeypatch.setattr(bootstrap.os, "lstat", lstat)
    assert execute(source, cache) == 17
    assert not list(cache.iterdir())


def test_unwritable_cache_falls_back(program, monkeypatch):
    source, cache = program
    def denied(*_a, **_kw):
        raise PermissionError("cache unavailable")
    monkeypatch.setattr(bootstrap.os, "mkdir", denied)
    assert execute(source, cache) == 17
    assert not cache.exists()


def test_failed_cache_write_still_runs_and_removes_temporary(program, monkeypatch):
    source, cache = program
    def denied(*_a, **_kw):
        raise PermissionError("cache write unavailable")
    monkeypatch.setattr(bootstrap.os, "replace", denied)
    assert execute(source, cache) == 17
    assert not list(cache.iterdir())


def test_symlink_cache_file_does_not_read_or_write_target(program, tmp_path):
    source, cache = program
    execute(source, cache)
    path = next(cache.glob("*.pyc"))
    path.unlink()
    target = tmp_path / "foreign-data"
    target.write_bytes(b"untouched")
    path.symlink_to(target)
    assert execute(source, cache) == 17
    assert target.read_bytes() == b"untouched"
    assert not path.is_symlink()


def test_fifo_cache_does_not_block(program):
    source, cache = program
    execute(source, cache)
    path = next(cache.glob("*.pyc"))
    path.unlink()
    os.mkfifo(str(path), mode=0o600)
    assert execute(source, cache) == 17
    assert path.is_file()


def test_concurrent_cold_starts_produce_complete_cache(program):
    source, cache = program
    with ThreadPoolExecutor(max_workers=6) as executor:
        assert list(executor.map(lambda _n: execute(source, cache), range(12))) == [17] * 12
    assert len(list(cache.iterdir())) == 1
    assert execute(source, cache) == 17


def test_disabled_cache_does_not_create_directory(program, monkeypatch):
    source, cache = program
    monkeypatch.setenv("CAMC_BYTECODE_CACHE_DISABLE", "1")
    assert execute(source, cache) == 17
    assert not cache.exists()


def test_bootstrap_preserves_argv_stdin_file_and_exit(tmp_path):
    source = tmp_path / "script with spaces"
    source.write_text(
        "import json, sys\n"
        "print(json.dumps([sys.argv, __file__, sys.stdin.read(), __name__]))\n"
        "sys.exit(7)\n")
    command = [sys.executable, "-c", Path(bootstrap.__file__).read_text(),
               str(source), "space argument", "quotes'\"", "--option"]
    for _n in range(2):
        result = subprocess.run(command, input="stdin stays intact\n", universal_newlines=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert result.returncode == 7 and not result.stderr
        assert json.loads(result.stdout) == [
            [str(source), "space argument", "quotes'\"", "--option"],
            str(source), "stdin stays intact\n", "__main__"]


def test_bootstrap_preserves_relative_script_argv(tmp_path):
    source = tmp_path / "entry"
    source.write_text("import sys\nprint(sys.argv[0])\n")
    result = subprocess.run([sys.executable, "-c", Path(bootstrap.__file__).read_text(),
                             "./entry"], cwd=str(tmp_path), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == 0 and result.stdout == "./entry\n"


def test_cache_never_freezes_runtime_environment(tmp_path):
    source = tmp_path / "entry"
    source.write_text("import os\nprint(os.environ['RUNTIME_VALUE'])\n")
    command = [sys.executable, "-c", Path(bootstrap.__file__).read_text(), str(source)]
    for value in ("first", "second"):
        output = subprocess.check_output(command, env=dict(os.environ, RUNTIME_VALUE=value))
        assert output.decode().strip() == value


def test_built_artifact_cached_and_uncached_output_match(tmp_path):
    spec = importlib.util.spec_from_file_location("cache_builder", ROOT / "build_camc.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    source = builder.build()
    ast.parse(source, feature_version=(3, 6))
    candidate = tmp_path / "camc with spaces"
    candidate.write_text(source)
    candidate.chmod(0o755)
    for args in (["run", "--help"], ["version", "--help"], ["invalid-subcommand"]):
        expected = subprocess.run([sys.executable, str(candidate)] + args,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for disabled in ("0", "0", "1"):
            env = dict(os.environ, CAMC_BYTECODE_CACHE_DISABLE=disabled)
            actual = subprocess.run([str(candidate)] + args, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert (actual.returncode, actual.stdout, actual.stderr) == (
                expected.returncode, expected.stdout, expected.stderr)
