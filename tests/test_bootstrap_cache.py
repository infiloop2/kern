from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from host.bootstrap import cache, render


class BootstrapCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / 'cache'
        self.cache = cache.Cache(self.root)
        self.target = self.directory / 'model.bin'
        self.content = b'pinned model bytes'
        self.checksum = hashlib.sha256(self.content).hexdigest()
        self.cached = self.root / 'models' / self.checksum
        self.url = 'https://example.invalid/model.bin'

    def download(self, args, **kwargs):
        Path(args[args.index('-o') + 1]).write_bytes(self.content)
        return subprocess.CompletedProcess(args, 0)

    def test_model_miss_then_fresh_root_disk_hit_without_network(self):
        with patch.object(cache.subprocess, 'run', side_effect=self.download) as download:
            self.cache.model(self.checksum, self.url, self.target)
        download.assert_called_once()
        self.assertEqual(self.cached.read_bytes(), self.content)
        self.target.unlink()
        with patch.object(cache.subprocess, 'run', side_effect=AssertionError('network on hit')):
            cache.Cache(self.root).model(self.checksum, self.url, self.target)
        self.assertEqual(self.target.read_bytes(), self.content)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.cached.stat().st_mode & 0o777, 0o600)

    def test_corrupt_model_is_downloaded_again(self):
        self.cached.write_bytes(b'broken')
        with patch.object(cache.subprocess, 'run', side_effect=self.download) as download:
            self.cache.model(self.checksum, self.url, self.target)
        download.assert_called_once()
        self.assertEqual(self.cached.read_bytes(), self.content)

    def test_bad_download_is_not_published(self):
        self.target.write_bytes(b'previous installation')
        with patch.object(cache.subprocess, 'run', side_effect=self.download):
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                self.cache.model('a' * 64, self.url, self.target)
        self.assertEqual(self.target.read_bytes(), b'previous installation')
        self.assertFalse((self.root / 'models' / ('a' * 64)).exists())
        self.assertEqual(list(self.directory.glob('.partial-*')), [])

    def test_interrupted_download_keeps_existing_cache_and_cleans_partial(self):
        self.cached.write_bytes(self.content)
        def fail(args, **kwargs):
            Path(args[args.index('-o') + 1]).write_bytes(b'incomplete')
            raise subprocess.CalledProcessError(22, args)
        with patch.object(cache.subprocess, 'run', side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                self.cache.model('b' * 64, self.url, self.target)
        self.assertEqual(self.cached.read_bytes(), self.content)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.directory.glob('.partial-*')), [])

    def test_low_space_skips_cache_but_installs_verified_model(self):
        usage = cache.shutil.disk_usage(self.root)._replace(free=cache.RESERVE_BYTES)
        with patch.object(cache.shutil, 'disk_usage', return_value=usage):
            with patch.object(cache.subprocess, 'run', side_effect=self.download):
                self.cache.model(self.checksum, self.url, self.target)
        self.assertEqual(self.target.read_bytes(), self.content)
        self.assertFalse(self.cached.exists())

    def test_size_limit_does_not_evict_valid_entries_before_success(self):
        self.cached.write_bytes(self.content)
        self.target.write_bytes(b'new bytes')
        with patch.object(cache, 'MAX_BYTES', len(self.content)):
            self.cache.save(self.target, self.root / 'models' / ('c' * 64))
        self.assertEqual(list((self.root / 'models').iterdir()), [self.cached])

    def test_refuses_symlinked_cache_directory(self):
        other = self.directory / 'other'
        other.mkdir()
        (self.root / 'apt').rmdir()
        (self.root / 'apt').symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'unsafe bootstrap cache'):
            cache.Cache(self.root)
        self.assertEqual(list(other.iterdir()), [])

    def test_model_symlink_is_replaced_without_touching_target(self):
        other = self.directory / 'other'
        other.write_bytes(b'untouched')
        self.cached.symlink_to(other)
        with patch.object(cache.subprocess, 'run', side_effect=self.download):
            self.cache.model(self.checksum, self.url, self.target)
        self.assertEqual(other.read_bytes(), b'untouched')
        self.assertFalse(self.cached.is_symlink())
        self.assertEqual(self.cached.read_bytes(), self.content)

    def test_deb_round_trip_skips_partial_and_unsafe_files(self):
        archives = self.directory / 'archives'
        archives.mkdir()
        good = archives / 'package_1_all.deb'
        good.write_bytes(b'archive')
        (archives / 'partial').mkdir()
        (archives / 'partial' / 'unfinished.deb').write_bytes(b'partial')
        (archives / 'linked.deb').symlink_to(good)
        self.cache.debs(archives, restore=False)
        self.assertEqual([p.name for p in (self.root / 'apt').iterdir()],
                         [f'{cache.digest(good)}_{good.name}'])
        good.unlink()
        self.cache.debs(archives, restore=True)
        self.assertEqual(good.read_bytes(), b'archive')
        self.assertEqual(good.stat().st_mode & 0o777, 0o644)

    def test_same_size_corrupt_deb_is_not_restored(self):
        archives = self.directory / 'archives'
        archives.mkdir()
        archive = archives / 'package_1_all.deb'
        archive.write_bytes(b'good')
        self.cache.debs(archives, restore=False)
        cached = next((self.root / 'apt').iterdir())
        cached.write_bytes(b'evil')
        archive.unlink()
        self.cache.debs(archives, restore=True)
        self.assertFalse(archive.exists())
        self.assertFalse(cached.exists())

    def test_prune_retains_installed_package_tuple_and_current_models(self):
        # Real dpkg-deb output: package identity includes architecture and version.
        package = self.directory / 'package'
        (package / 'DEBIAN').mkdir(parents=True)
        debs = self.root / 'apt'
        for version, arch in [('1', 'all'), ('2', 'all'), ('2', 'arm64')]:
            (package / 'DEBIAN' / 'control').write_text(
                f'Package: cache-test\nVersion: {version}\nArchitecture: {arch}\n'
                'Maintainer: Test <test@example.invalid>\nDescription: cache test\n')
            subprocess.run(['dpkg-deb', '--build', str(package),
                            str(debs / f'cache-test_{version}_{arch}.deb')],
                           check=True, capture_output=True)
        self.cached.write_bytes(self.content)
        obsolete = self.root / 'models' / ('d' * 64)
        obsolete.write_bytes(b'old model')
        (debs / 'broken.deb').write_bytes(b'broken')
        run = subprocess.run
        def query(args, **kwargs):
            if args[0] == 'dpkg-query':
                return subprocess.CompletedProcess(args, 0, 'cache-test\t2\tall\tinstalled\n')
            return run(args, **kwargs)
        with patch.object(cache.subprocess, 'run', side_effect=query):
            self.cache.prune({self.checksum})
        self.assertEqual([p.name for p in debs.iterdir()], ['cache-test_2_all.deb'])
        self.assertEqual(list((self.root / 'models').iterdir()), [self.cached])

    def test_abandoned_cache_partial_is_removed_on_next_bootstrap(self):
        partial = self.root / 'models' / '.partial-interrupted'
        partial.write_bytes(b'partial')
        self.cached.write_bytes(self.content)
        cache.Cache(self.root)
        self.assertFalse(partial.exists())
        self.assertEqual(self.cached.read_bytes(), self.content)

    def test_bootstrap_mounts_before_cache_and_prunes_only_after_verification(self):
        script = render._render_bootstrap()
        main = script.split('main() {', 1)[1]
        self.assertLess(main.index('mount_durable_volumes'), main.index('install_system_packages'))
        self.assertLess(main.index('install_runtime_code'), main.index('install_system_packages'))
        self.assertLess(main.index('verify_deployment'), main.index('bootstrap_cache prune'))
        self.assertLess(main.index('bootstrap_cache prune'), main.index('finalize_deploy'))
        self.assertIn('bootstrap_cache restore-debs\napt_get update', script)
        self.assertEqual(script.count('bootstrap_cache save-debs'), 3)
        self.assertEqual(script.count('bootstrap_cache model "$_digest"'), 2)


if __name__ == '__main__':
    unittest.main()
