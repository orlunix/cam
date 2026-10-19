"""Small Python entry embedded in the standalone shell launcher."""

import hashlib
import importlib.machinery
import importlib.util
import marshal
import os
import stat
import struct
import sys
import tempfile
import types


def _camc_cached_code(source, cache_dir=None):
    # Hash the bytes we actually compile, including the embedded launcher.
    # Path matters too: code objects retain their original filename.
    with open(source, "rb") as stream:
        data = stream.read()
    header = importlib.util.MAGIC_NUMBER
    if sys.version_info >= (3, 7):
        header += struct.pack("<III", 0, 0, len(data))
    else:
        header += struct.pack("<II", 0, len(data))
    identity = (source + "\0" + sys.version + "\0" +
                str(sys.flags.optimize)).encode("utf-8", "surrogateescape")
    key = hashlib.sha256(identity + header + data).hexdigest()
    code = None
    temporary = None
    try:
        if os.environ.get("CAMC_BYTECODE_CACHE_DISABLE") == "1":
            raise OSError("bytecode cache disabled")
        directory = cache_dir or "/tmp/camc-bytecode-%d" % os.getuid()
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(directory)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise OSError("unsafe cache directory")
        cache = os.path.join(directory, key + ".pyc")
        try:
            fd = os.open(cache, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077
                        or stream.read(len(header)) != header):
                    raise ValueError("unsafe or incompatible cache file")
                payload = stream.read()
                encoded, checksum = payload[:-32], payload[-32:]
                if hashlib.sha256(key.encode("ascii") + header + encoded).digest() != checksum:
                    raise ValueError("damaged cached code")
                code = marshal.loads(encoded)
                if not isinstance(code, types.CodeType):
                    code = None
                    raise ValueError("invalid cached code")
            return code
        except (OSError, ValueError, EOFError, TypeError, OverflowError, RecursionError):
            pass
        code = compile(data, source, "exec", dont_inherit=True)
        fd, temporary = tempfile.mkstemp(prefix=key + ".", dir=directory)
        with os.fdopen(fd, "wb") as stream:
            encoded = marshal.dumps(code)
            stream.write(header)
            stream.write(encoded)
            stream.write(hashlib.sha256(key.encode("ascii") + header + encoded).digest())
        os.replace(temporary, cache)
        temporary = None
    except (OSError, ValueError, EOFError, TypeError):
        # Cache setup/read/write failure must not prevent a normal launch.
        pass
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return code if code is not None else compile(data, source, "exec", dont_inherit=True)


def _camc_bytecode_main():
    script = sys.argv[1]
    source = os.path.abspath(script)
    sys.argv = [script] + sys.argv[2:]
    sys.path[0] = os.path.dirname(source)
    code = _camc_cached_code(source)
    # Match direct script execution: argv[0], __file__, __main__ globals,
    # exit codes, and the source path used to spawn monitors/workers.
    main = types.ModuleType("__main__")
    main.__file__ = source if sys.version_info >= (3, 9) else script
    main.__loader__ = importlib.machinery.SourceFileLoader("__main__", source)
    main.__package__ = None
    main.__spec__ = None
    main.__cached__ = None
    sys.modules["__main__"] = main
    exec(code, main.__dict__)


if __name__ == "__main__":
    _camc_bytecode_main()
