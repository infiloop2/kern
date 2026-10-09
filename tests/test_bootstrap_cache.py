from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
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
        self.assertEqual(script.count('bootstrap_cache save-debs'), 4)
        self.assertEqual(script.count('bootstrap_cache model "$_digest"'), 2)
        self.assertLess(main.index('verify_deployment'), main.index('bootstrap_cache prune-browser'))
        self.assertLess(main.index('verify_deployment'), main.index('bootstrap_cache prune-packages'))

    def test_package_downloads_survive_root_replacement_without_live_installs(self):
        source = self.browser_downloads()
        (source / 'browsers').rename(source / 'npm')
        key = cache.packages_key('0.9.26')
        self.cache.save_downloads('packages', key, source, ('npm', 'uv'))
        shutil.rmtree(source)
        restored = self.directory / 'new-root'
        restored.mkdir()
        self.assertTrue(cache.Cache(self.root).restore_downloads('packages', key, restored, ('npm', 'uv')))
        self.assertEqual((restored / 'uv' / 'absolute-link').read_bytes(), b'Python package')
        self.assertEqual((restored / 'npm' / 'chromium' / 'chrome').read_bytes(), b'browser binary')
        self.assertEqual(set(p.name for p in restored.iterdir()), {'npm', 'uv', '.restored-files.json'})
        self.assertEqual((self.root / 'packages').stat().st_mode & 0o777, 0o700)

    def test_corrupt_package_archive_is_a_miss_and_preserves_browser_cache(self):
        source = self.browser_downloads()
        self.cache.save_browser('browser', source)
        browser_archives = list((self.root / 'browser').iterdir())
        (source / 'browsers').rename(source / 'npm')
        self.cache.save_downloads('packages', 'packages', source, ('npm', 'uv'))
        archive = next((self.root / 'packages').iterdir())
        archive.write_bytes(b'corrupt')
        restored = self.directory / 'new-root'
        restored.mkdir()
        self.assertFalse(self.cache.restore_downloads('packages', 'packages', restored, ('npm', 'uv')))
        self.assertFalse(archive.exists())
        self.assertEqual(list((self.root / 'browser').iterdir()), browser_archives)

    def test_package_archive_space_limit_and_pruning_preserve_other_downloads(self):
        source = self.browser_downloads()
        (source / 'browsers').rename(source / 'npm')
        self.cache.save_downloads('packages', 'old', source, ('npm', 'uv'))
        old = next((self.root / 'packages').iterdir())
        with patch.object(cache, 'MAX_BYTES', old.stat().st_size):
            self.cache.save_downloads('packages', 'new', source, ('npm', 'uv'))
        self.assertEqual(list((self.root / 'packages').iterdir()), [old])
        self.cache.save_downloads('packages', 'new', source, ('npm', 'uv'))
        self.cached.write_bytes(self.content)
        self.cache.prune_downloads('packages', 'new')
        self.assertFalse(old.exists())
        self.assertTrue(self.cached.exists())
        self.assertEqual(len(list((self.root / 'packages').iterdir())), 1)

    def test_unchanged_package_downloads_do_not_rebuild_large_snapshot(self):
        source = self.browser_downloads()
        (source / 'browsers').rename(source / 'npm')
        self.cache.save_downloads('packages', 'key', source, ('npm', 'uv'))
        restored = self.directory / 'new-root'
        restored.mkdir()
        self.assertTrue(self.cache.restore_downloads('packages', 'key', restored, ('npm', 'uv')))
        (restored / 'npm' / '_logs').mkdir()
        (restored / 'npm' / '_logs' / 'new.log').write_text('installer log')
        with patch.object(cache.tarfile, 'open', side_effect=AssertionError('rebuilt unchanged snapshot')):
            self.cache.save_downloads('packages', 'key', restored, ('npm', 'uv'))
        (restored / 'uv' / 'new-package').write_bytes(b'new package')
        with patch.object(self.cache, 'save') as save:
            self.cache.save_downloads('packages', 'key', restored, ('npm', 'uv'))
        save.assert_called_once()

    def test_download_archive_budget_is_checked_before_building(self):
        source = self.browser_downloads()
        (source / 'browsers').rename(source / 'npm')
        with patch.object(cache, 'MAX_BYTES', 1), patch.object(cache.tarfile, 'open', side_effect=AssertionError('built oversized archive')):
            self.cache.save_downloads('packages', 'key', source, ('npm', 'uv'))
        self.assertEqual(list((self.root / 'packages').iterdir()), [])

    def test_download_inventory_excludes_logs_and_does_not_follow_directory_links(self):
        source = self.browser_downloads()
        (source / 'browsers').rename(source / 'npm')
        (source / 'npm' / '_logs').mkdir()
        (source / 'npm' / '_logs' / 'run.log').write_text('installer log')
        files = self.cache.download_files(source, ('npm', 'uv'))
        self.assertIn('npm/chromium/chrome', files)
        self.assertIn('uv/wheel-link', files)
        self.assertIn('uv/archive/wheel', files)
        self.assertNotIn('npm/_logs/run.log', files)
        self.assertNotIn('uv/wheels/index/package/version/wheel', files)

    def test_package_install_refreshes_metadata_and_propagates_network_failure(self):
        script = render._render_bootstrap()
        helpers = 'npm_install() {' + script.split('npm_install() {', 1)[1].split('install_agent_clis() {', 1)[0]
        fake = "npm() { echo npm \"$@\"; return \"$FAKE_STATUS\"; }\nuv() { echo uv \"$@\"; return \"$FAKE_STATUS\"; }\n"
        for command, option in (('npm_install install package@1', '--prefer-online'),
                                ('python_packages_install /fixture/python package==1', '--refresh')):
            for status in (0, 7):
                with self.subTest(command=command, status=status):
                    result = subprocess.run(['bash', '-c', f'set -eu\nFAKE_STATUS={status}\n' + helpers + fake + command], capture_output=True, text=True)
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertIn(option, result.stdout)
                    self.assertNotIn('--offline', result.stdout)

    def test_phase_timing_does_not_swallow_bootstrap_failure(self):
        script = render._render_bootstrap()
        helper = 'bootstrap_phase() {' + script.split('bootstrap_phase() {', 1)[1].split('main() {', 1)[0]
        for status in (0, 7):
            result = subprocess.run(['bash', '-c', f'set -eu\n{helper}\nphase() {{ return {status}; }}\nbootstrap_phase phase\necho continued'], capture_output=True, text=True)
            self.assertEqual(result.returncode, status)
            self.assertIn('phase start ', result.stderr)
            self.assertEqual('phase complete ' in result.stderr, status == 0)
            self.assertEqual('continued' in result.stdout, status == 0)

    def browser_downloads(self):
        source = self.directory / 'downloads'
        (source / 'uv' / 'archive').mkdir(parents=True)
        (source / 'uv' / 'archive' / 'wheel').write_bytes(b'Python package')
        os.link(source / 'uv' / 'archive' / 'wheel', source / 'uv' / 'archive' / 'hardlink')
        (source / 'uv' / 'wheel-link').symlink_to('archive/wheel')
        (source / 'uv' / 'absolute-link').symlink_to(source / 'uv' / 'archive' / 'wheel')
        (source / 'uv' / 'wheels' / 'index' / 'package').mkdir(parents=True)
        (source / 'uv' / 'wheels' / 'index' / 'package' / 'version').symlink_to('../../../archive')
        (source / 'browsers' / 'chromium').mkdir(parents=True)
        binary = source / 'browsers' / 'chromium' / 'chrome'
        binary.write_bytes(b'browser binary')
        binary.chmod(0o755)
        return source

    def test_browser_download_cache_survives_fresh_root_with_links_and_executables(self):
        source = self.browser_downloads()
        key = cache.browser_key('1.60.0', '0.9.26')
        self.cache.save_browser(key, source)
        shutil.rmtree(source)
        restored = self.directory / 'fresh-root'
        restored.mkdir()
        self.assertTrue(cache.Cache(self.root).restore_browser(key, restored))
        self.assertEqual((restored / 'uv' / 'wheel-link').read_bytes(), b'Python package')
        self.assertTrue((restored / 'uv' / 'wheel-link').is_symlink())
        self.assertEqual((restored / 'uv' / 'archive' / 'hardlink').stat().st_ino,
                         (restored / 'uv' / 'archive' / 'wheel').stat().st_ino)
        self.assertEqual((restored / 'uv' / 'absolute-link').read_bytes(), b'Python package')
        self.assertFalse((restored / 'uv' / 'absolute-link').readlink().is_absolute())
        self.assertEqual((restored / 'uv' / 'wheels' / 'index' / 'package' / 'version' / 'wheel').read_bytes(), b'Python package')
        binary = restored / 'browsers' / 'chromium' / 'chrome'
        self.assertEqual(binary.read_bytes(), b'browser binary')
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
        archive = next((self.root / 'browser').iterdir())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(restored.iterdir()), {restored / 'uv', restored / 'browsers'})

    def test_browser_root_install_survives_staging_removal_with_copy_fallback(self):
        script = render._render_bootstrap()
        copy = script.split('  install -d -m 0755 /usr/local/share/kern-browsers\n', 1)[1].split('  if [ "$browser_cache_hit"', 1)[0]
        copy = copy.replace('/usr/local/share/kern-browsers/', '"$browser_installed"/')
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory(dir=self.directory) as directory:
                root = Path(directory)
                downloads = root / 'staging'
                (downloads / 'browsers' / 'chromium').mkdir(parents=True)
                binary = downloads / 'browsers' / 'chromium' / 'chrome'
                binary.write_bytes(b'new root browser')
                binary.chmod(0o755)
                inode = binary.stat().st_ino
                installed = root / 'installed'
                installed.mkdir()
                wrapper = 'cp() { if [ "$1" = -al ]; then return 1; fi; /usr/bin/cp "$@"; }\n' if fallback else ''
                subprocess.run(['bash', '-c', 'set -eu\n' + wrapper + copy], check=True,
                               env={**os.environ, 'browser_downloads': str(downloads), 'browser_installed': str(installed)})
                shutil.rmtree(downloads)
                runtime = installed / 'chromium' / 'chrome'
                self.assertEqual(runtime.read_bytes(), b'new root browser')
                self.assertEqual(runtime.stat().st_mode & 0o777, 0o755)
                self.assertEqual(runtime.stat().st_ino == inode, not fallback)

    def test_corrupt_browser_archive_is_a_miss_without_partial_restoration(self):
        self.cache.save_browser('current', self.browser_downloads())
        archive = next((self.root / 'browser').iterdir())
        archive.write_bytes(b'corrupted')
        target = self.directory / 'restored'
        target.mkdir()
        self.assertFalse(self.cache.restore_browser('current', target))
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(archive.exists())

    def test_browser_cache_key_changes_with_runtime_compatibility(self):
        original = cache.browser_key('1.60.0', '0.9.26')
        self.assertNotEqual(original, cache.browser_key('1.61.0', '0.9.26'))
        self.assertNotEqual(original, cache.browser_key('1.60.0', '0.10.0'))
        with patch.object(cache.sysconfig, 'get_platform',
                          return_value=cache.sysconfig.get_platform() + '-different'):
            self.assertNotEqual(original, cache.browser_key('1.60.0', '0.9.26'))
        with patch.object(cache.sys.implementation, 'cache_tag',
                          (cache.sys.implementation.cache_tag or '') + '-different'):
            self.assertNotEqual(original, cache.browser_key('1.60.0', '0.9.26'))
        release = cache.platform.freedesktop_os_release()
        with patch.object(cache.platform, 'freedesktop_os_release',
                          return_value={**release, 'VERSION_ID': release['VERSION_ID'] + '-different'}):
            self.assertNotEqual(original, cache.browser_key('1.60.0', '0.9.26'))

    def test_browser_archive_rejects_paths_links_and_special_files(self):
        for name, kind, link in [('uv/../../outside', tarfile.REGTYPE, ''),
                                 ('uv/link', tarfile.SYMTYPE, '../../outside'),
                                 ('uv/absolute', tarfile.SYMTYPE, '/outside'),
                                 ('uv/hardlink', tarfile.LNKTYPE, '../outside'),
                                 ('uv/device', tarfile.CHRTYPE, '')]:
            with self.subTest(name=name):
                archive = self.directory / 'unsafe.tar'
                with tarfile.open(archive, 'w') as bundle:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = link
                    bundle.addfile(member)
                self.cache.save(archive, self.root / 'browser' / f'current_{cache.digest(archive)}.tar')
                target = self.directory / 'restore'
                target.mkdir(exist_ok=True)
                self.assertFalse(self.cache.restore_browser('current', target))
                self.assertEqual(list(target.iterdir()), [])
                self.assertFalse((self.directory / 'outside').exists())

    def test_browser_archive_uses_shared_size_and_free_space_limits(self):
        source = self.browser_downloads()
        self.cached.write_bytes(self.content)
        with patch.object(cache, 'MAX_BYTES', len(self.content)):
            self.cache.save_browser('current', source)
        self.assertEqual(list((self.root / 'browser').iterdir()), [])
        usage = cache.shutil.disk_usage(self.root)._replace(free=cache.RESERVE_BYTES)
        with patch.object(cache.shutil, 'disk_usage', return_value=usage):
            self.cache.save_browser('current', source)
        self.assertEqual(list((self.root / 'browser').iterdir()), [])
        self.assertTrue((source / 'browsers' / 'chromium' / 'chrome').exists())
        self.assertEqual(self.cached.read_bytes(), self.content)

    def test_browser_archives_retained_until_success_then_only_current_newest_kept(self):
        source = self.browser_downloads()
        self.cache.save_browser('old', source)
        self.cache.save_browser('current', source)
        older = next((self.root / 'browser').glob('current_*'))
        os.utime(older, (1, 1))
        (source / 'uv' / 'archive' / 'wheel').write_bytes(b'new package')
        self.cache.save_browser('current', source)
        self.assertEqual(len(list((self.root / 'browser').iterdir())), 3)
        self.cache.prune_browser('current')
        remaining = list((self.root / 'browser').iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0].name.startswith('current_'))
        self.assertNotEqual(remaining[0], older)

    def test_browser_archive_failed_publication_keeps_completed_cache(self):
        source = self.browser_downloads()
        self.cache.save_browser('old', source)
        before = list((self.root / 'browser').iterdir())
        with patch.object(cache.os, 'replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.cache.save_browser('current', source)
        self.assertEqual(list((self.root / 'browser').iterdir()), before)


if __name__ == '__main__':
    unittest.main()
