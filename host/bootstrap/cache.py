"""Disposable bootstrap downloads on the preserved admin volume.

Only bootstrap writes this directory. Runtime services continue reading their
normal root-volume installations. APT still refreshes/authenticates its indexes
and selects packages; this is an archive cache, not an offline repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile


CACHE_ROOT = Path('/mnt/kern-admin/bootstrap-cache')
APT_ARCHIVES = Path('/var/cache/apt/archives')
MAX_BYTES = 2 * 1024**3
RESERVE_BYTES = 1024**3


def compatibility_key(uv_version: str) -> str:
    release = platform.freedesktop_os_release()
    identity = '|'.join((uv_version, sys.implementation.cache_tag or '',
                         sysconfig.get_platform(), release['ID'], release['VERSION_ID']))
    return hashlib.sha256(identity.encode()).hexdigest()


def browser_key(version: str, uv_version: str) -> str:
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('invalid Playwright version')
    return f'playwright-{version}-{compatibility_key(uv_version)}'


def packages_key(uv_version: str) -> str:
    return f'packages-{compatibility_key(uv_version)}'


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and not info.st_mode & 0o022 and info.st_nlink == 1)


def extract_downloads(archive: tarfile.TarFile, destination: Path, names: tuple[str, ...]) -> None:
    """Extract only cache files/internal links, including on older Ubuntu Python."""
    root = destination.resolve()
    for member in archive:
        parts = Path(member.name).parts
        if not parts or parts[0] not in names or '..' in parts:
            raise ValueError('invalid download cache member')
        target = destination / member.name
        target.resolve().relative_to(root)
        if member.isdir():
            if target.is_symlink():
                raise ValueError('download cache directory is a link')
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists() or target.is_symlink():
            raise ValueError('duplicate download cache member')
        target.parent.mkdir(parents=True, exist_ok=True)
        if member.isfile():
            content = archive.extractfile(member)
            if content is None:
                raise ValueError('missing download cache file')
            with content, target.open('xb') as output:
                shutil.copyfileobj(content, output)
            target.chmod((member.mode & 0o755) | 0o600)
        elif member.issym() or member.islnk():
            if os.path.isabs(member.linkname):
                raise ValueError('absolute download cache link')
            parent = target.parent if member.issym() else destination
            link = (parent / member.linkname).resolve()
            link.relative_to(root)
            if member.issym():
                target.symlink_to(member.linkname)
            elif link.is_file():
                os.link(link, target)
            else:
                raise ValueError('missing download cache hardlink target')
        else:
            raise ValueError('special download cache file')


class Cache:
    def __init__(self, root: Path = CACHE_ROOT):
        self.root = root
        for directory in (root, root / 'apt', root / 'models', root / 'browser', root / 'packages'):
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077):
                raise RuntimeError(f'unsafe bootstrap cache directory: {directory}')
            for entry in directory.iterdir():
                if entry.name.startswith('.partial-'):
                    entry.unlink()

    def save(self, source: Path, destination: Path) -> None:
        if regular(destination) and digest(destination) == digest(source):
            return
        size = source.stat().st_size
        if not self.can_save(size):
            print(f'Bootstrap cache: not saving {source.name}; preserving disk space', flush=True)
            return
        self.copy(source, destination)

    def can_save(self, size: int) -> bool:
        used = sum(p.lstat().st_size for group in ('apt', 'models', 'browser', 'packages')
                   for p in (self.root / group).iterdir())
        return used + size <= MAX_BYTES and shutil.disk_usage(self.root).free >= size + RESERVE_BYTES

    @staticmethod
    def download_files(source: Path, names: tuple[str, ...]) -> set[str]:
        # New immutable artifact paths require a new snapshot. Metadata is
        # revalidated online every time; npm's per-run logs are not downloads.
        result: set[str] = set()
        for name in names:
            for parent, directories, files in os.walk(source / name):
                if Path(parent) == source / 'npm':
                    directories[:] = [directory for directory in directories if directory != '_logs']
                # walk uses scandir's directory-entry types, avoiding a disk
                # stat for each file in large unpacked uv package archives.
                result.update(str((Path(parent) / filename).relative_to(source)) for filename in files)
        return result

    @staticmethod
    def copy(source: Path, destination: Path) -> None:
        # Publish only complete files, without following an existing target link.
        fd, name = tempfile.mkstemp(prefix='.partial-', dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as target, source.open('rb') as origin:
                shutil.copyfileobj(origin, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def debs(self, archives: Path = APT_ARCHIVES, *, restore: bool) -> None:
        source, target = (self.root / 'apt', archives) if restore else (archives, self.root / 'apt')
        count = 0
        for entry in source.glob('*.deb'):
            if regular(entry):
                if restore:
                    checksum, _, filename = entry.name.partition('_')
                    # APT can reuse same-sized archives without hashing them.
                    # Verify our saved bytes before putting them in its cache.
                    if (not re.fullmatch('[0-9a-f]{64}', checksum)
                            or not filename or digest(entry) != checksum):
                        print(f'Bootstrap cache: discarding corrupt archive {entry.name}', flush=True)
                        entry.unlink()
                        continue
                    self.copy(entry, target / filename)
                    # _apt must be able to read the restored package.
                    (target / filename).chmod(0o644)
                else:
                    self.save(entry, target / f'{digest(entry)}_{entry.name}')
                count += 1
        print(f'Bootstrap cache: {"restored" if restore else "processed"} {count} apt archives', flush=True)

    def model(self, checksum: str, url: str, target: Path) -> None:
        if not re.fullmatch('[0-9a-f]{64}', checksum):
            raise ValueError('invalid model SHA256')
        cached = self.root / 'models' / checksum
        if regular(cached) and digest(cached) == checksum:
            print(f'Bootstrap cache: model hit {target.name}', flush=True)
            self.copy(cached, target)
            return
        print(f'Bootstrap cache: downloading model {target.name}', flush=True)
        # Download on the disposable root disk, keeping durable DB space free.
        fd, name = tempfile.mkstemp(prefix='.partial-', dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            subprocess.run(['curl', '-fsSL', '--retry', '5', '--retry-all-errors',
                            '--retry-delay', '2', '--connect-timeout', '20', '--max-time', '600',
                            '--proto', '=https', '--proto-redir', '=https',
                            '-o', str(temporary), url], check=True)
            if digest(temporary) != checksum:
                raise ValueError(f'model checksum mismatch: {target.name}')
            os.replace(temporary, target)
            self.save(target, cached)
        finally:
            temporary.unlink(missing_ok=True)

    def restore_downloads(self, group: str, key: str, target: Path, names: tuple[str, ...]) -> bool:
        """Restore download caches onto the disposable root disk, never live venvs."""
        for entry in (self.root / group).glob(f'{key}_*.tar'):
            checksum = entry.stem.removeprefix(f'{key}_')
            if (not regular(entry) or not re.fullmatch('[0-9a-f]{64}', checksum)
                    or digest(entry) != checksum):
                print(f'Bootstrap cache: discarding corrupt {group} archive', flush=True)
                entry.unlink()
                continue
            try:
                # Extract into a private staging directory. Reject paths outside
                # the two download caches, devices and escaping symlinks.
                with tempfile.TemporaryDirectory(dir=target) as staging:
                    with tarfile.open(entry) as archive:
                        extract_downloads(archive, Path(staging), names)
                    for name in names:
                        source = Path(staging) / name
                        if source.is_symlink() or not source.is_dir():
                            raise ValueError('incomplete download cache')
                    for name in names:
                        shutil.move(str(Path(staging) / name), target / name)
            except (ValueError, tarfile.TarError, EOFError):
                print(f'Bootstrap cache: discarding invalid {group} archive', flush=True)
                entry.unlink()
                continue
            print(f'Bootstrap cache: {group} hit', flush=True)
            if group == 'packages':
                (target / '.restored-files.json').write_text(json.dumps(sorted(self.download_files(target, names))))
            return True
        print(f'Bootstrap cache: {group} miss', flush=True)
        return False

    def save_downloads(self, group: str, key: str, source: Path, names: tuple[str, ...]) -> None:
        inventory = source / '.restored-files.json'
        if group == 'packages' and regular(inventory):
            previous = set(json.loads(inventory.read_text()))
            if self.download_files(source, names) <= previous:
                print('Bootstrap cache: packages unchanged; retaining verified snapshot', flush=True)
                return
        # Check the budget before reading/copying hundreds of MB into a tar
        # that save() would discard. Include generous tar/PAX header overhead;
        # count hardlinked payloads only once, as tarfile does.
        estimate = 0
        seen = set()
        for name in names:
            for path in (source / name).rglob('*'):
                estimate += 2048
                info = path.lstat()
                identity = (info.st_dev, info.st_ino)
                if stat.S_ISREG(info.st_mode) and identity not in seen:
                    estimate += info.st_size
                    seen.add(identity)
        if not self.can_save(estimate):
            print(f'Bootstrap cache: not building {group} archive; preserving disk space', flush=True)
            return
        # Build the archive on the disposable root volume. Only a complete
        # download is published atomically, under the shared space limits.
        with tempfile.TemporaryDirectory() as staging:
            archive_path = Path(staging) / f'{group}.tar'
            def portable_link(member: tarfile.TarInfo) -> tarfile.TarInfo:
                # uv indexes use absolute symlinks into archive-v0. Rewrite
                # internal links so a fresh staging path can reuse the cache.
                if member.issym():
                    target = (source / member.name).resolve(strict=True)
                    target.relative_to(source.resolve())  # Refuse external links.
                    member.linkname = os.path.relpath(target, (source / member.name).parent)
                return member
            with tarfile.open(archive_path, 'w') as archive:
                for name in names:
                    archive.add(source / name, arcname=name, filter=portable_link)
            self.save(archive_path, self.root / group / f'{key}_{digest(archive_path)}.tar')

    def prune_downloads(self, group: str, key: str) -> None:
        matching = [entry for entry in (self.root / group).glob(f'{key}_*.tar')
                    if regular(entry)]
        newest = max(matching, key=lambda entry: entry.stat().st_mtime, default=None)
        for entry in (self.root / group).iterdir():
            if entry != newest:
                entry.unlink()

    def restore_browser(self, key: str, target: Path) -> bool:
        return self.restore_downloads('browser', key, target, ('uv', 'browsers'))

    def save_browser(self, key: str, source: Path) -> None:
        self.save_downloads('browser', key, source, ('uv', 'browsers'))

    def prune_browser(self, key: str) -> None:
        self.prune_downloads('browser', key)

    def prune(self, model_checksums: set[str]) -> None:
        """Called only after deploy verification; keep installed package versions."""
        installed = subprocess.run(
            ['dpkg-query', '-W', '-f=${Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n'],
            check=True, capture_output=True, text=True,
        ).stdout
        keep = {tuple(row.split('\t')[:3]) for row in installed.splitlines()
                if row.endswith('\tinstalled')}
        for entry in (self.root / 'apt').iterdir():
            if regular(entry) and entry.suffix == '.deb':
                info = subprocess.run(['dpkg-deb', '-f', str(entry), 'Package', 'Version', 'Architecture'],
                                      capture_output=True, text=True)
                fields = tuple(line.partition(': ')[2] for line in info.stdout.splitlines())
                if info.returncode == 0 and fields in keep:
                    continue
            entry.unlink()
        for entry in (self.root / 'models').iterdir():
            if entry.name not in model_checksums or not regular(entry):
                entry.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('restore-debs', 'save-debs', 'model', 'prune',
                                         'restore-browser', 'save-browser', 'prune-browser',
                                         'restore-packages', 'save-packages', 'prune-packages'))
    parser.add_argument('args', nargs='*')
    args = parser.parse_args()
    cache = Cache()
    if args.action == 'restore-debs':
        cache.debs(restore=True)
    elif args.action == 'save-debs':
        cache.debs(restore=False)
    elif args.action == 'model':
        checksum, url, target = args.args
        cache.model(checksum, url, Path(target))
    elif args.action in ('restore-browser', 'save-browser', 'prune-browser'):
        version, uv_version = args.args[:2]
        key = browser_key(version, uv_version)
        if args.action == 'restore-browser':
            if not cache.restore_browser(key, Path(args.args[2])):
                raise SystemExit(3)  # Cache miss; other failures must stop bootstrap.
        elif args.action == 'save-browser':
            cache.save_browser(key, Path(args.args[2]))
        else:
            cache.prune_browser(key)
    elif args.action in ('restore-packages', 'save-packages', 'prune-packages'):
        key = packages_key(args.args[0])
        if args.action == 'restore-packages':
            if not cache.restore_downloads('packages', key, Path(args.args[1]), ('npm', 'uv')):
                raise SystemExit(3)
        elif args.action == 'save-packages':
            cache.save_downloads('packages', key, Path(args.args[1]), ('npm', 'uv'))
        else:
            cache.prune_downloads('packages', key)
    else:
        cache.prune(set(args.args))


if __name__ == '__main__':
    main()
