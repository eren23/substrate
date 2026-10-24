"""Remove runtime-owned mounts only after sandbox and process teardown."""
from pathlib import Path
import re


def mounts_under(root, mountinfo):
    root = Path(root)
    if not root.is_absolute() or root == Path('/') or '..' in root.parts:
        raise ValueError('runtime cleanup requires a specific absolute root')
    points = set()
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 10 or ' - ' not in line:
            raise ValueError('malformed mountinfo')
        point = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
        if point == root or root in point.parents:
            points.add(str(point))
    return sorted(points, key=lambda p: (-len(Path(p).parts), p))


def unmount_runtime(root, unmount, read_mountinfo=None, *, processes_stopped):
    # runsc delete/list can leave its null-netns bind mount behind. Its own
    # state can be empty while rmtree still fails with EBUSY.
    if processes_stopped is not True:
        raise RuntimeError('owned process groups were not verified stopped')
    read_mountinfo = read_mountinfo or (lambda: Path('/proc/self/mountinfo').read_text())
    points = mounts_under(root, read_mountinfo())
    for point in points:
        unmount(point)
    remaining = mounts_under(root, read_mountinfo())
    if remaining:
        raise RuntimeError('owned runtime mounts remain: ' + repr(remaining))
    return {'root': str(root), 'unmounted': points, 'remaining_mounts': remaining}
