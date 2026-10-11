"""Verify pre-established kernel cgroup-v2 resource limits; never change the host.

The command may be launched in a resource-restricted systemd user scope. Limits
are inherited by the broker and all renderer descendants. The renderer uses a
strict cgroup namespace under an existing nsdelegate-enabled cgroup2 mount.
Polling RSS or V8 heap flags are not substitutes.
"""
from pathlib import Path
import os

from .config import PolicyError


def envelope(memory_bytes=1024 * 1024 * 1024, tasks=256):
    message = ('Secure browser capture requires cgroup-v2 MemoryMax<=1GiB, '
               'MemorySwapMax=0 and TasksMax<=256, plus existing cgroup2 nsdelegate; '
               'use the documented restricted user scope')
    try:
        lines = Path('/proc/self/mountinfo').read_text().splitlines()
        mount = next(line.split() for line in lines if ' - cgroup2 ' in line)
        # Unknown cgroup mount mappings fail closed rather than guessing ancestry.
        separator = mount.index('-')
        if (mount[3] != '/' or mount[4] != '/sys/fs/cgroup'
                or 'nsdelegate' not in mount[separator + 3].split(',')):
            raise PolicyError(message)
        groups = Path('/proc/self/cgroup').read_text().splitlines()
        group = next(line[3:] for line in groups if line.startswith('0::'))
        relative = Path(group.lstrip('/'))
        if '..' in relative.parts:
            raise PolicyError(message)
        root = Path('/sys/fs/cgroup')
        path = root / relative
        if path.is_symlink() or not path.is_dir():
            raise PolicyError(message)
        if str(os.getpid()) not in (path / 'cgroup.procs').read_text().split():
            raise PolicyError(message)
        found = {'memory.max': [], 'memory.swap.max': [], 'pids.max': []}
        for ancestor in [path, *path.parents]:
            if ancestor != root and root not in ancestor.parents:
                break
            for name in found:
                file = ancestor / name
                if file.exists():
                    value = file.read_text().strip()
                    if value != 'max':
                        found[name].append(int(value))
            if ancestor == root:
                break
        effective = {name: min(values) if values else None for name, values in found.items()}
        if (effective['memory.max'] is None or not 0 < effective['memory.max'] <= memory_bytes
                or effective['memory.swap.max'] != 0
                or effective['pids.max'] is None or not 0 < effective['pids.max'] <= tasks):
            raise PolicyError(message)
        return effective
    except (OSError, StopIteration, ValueError, IndexError) as exc:
        raise PolicyError(message) from exc
