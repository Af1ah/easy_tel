"""Exercise the updater against real Git histories with fake VPS services/pip."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy' / 'update.sh'


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / 'origin'
        self.source = self.root / 'source'
        self.app = self.root / 'app'
        self.source.mkdir()
        self.git(self.root, 'init', '--bare', str(self.origin))
        self.git(self.source, 'init', '-b', 'main')
        self.git(self.source, 'config', 'user.name', 'Deploy test')
        self.git(self.source, 'config', 'user.email', 'test@example.invalid')
        (self.source / '.gitignore').write_text('.venv/\n.deploy.lock\n')
        (self.source / 'requirements.txt').write_text('# version one\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '-m', 'one')
        self.old = self.git(self.source, 'rev-parse', 'HEAD')
        self.git(self.source, 'remote', 'add', 'origin', str(self.origin))
        self.git(self.source, 'push', 'origin', 'main')
        self.git(self.root, 'clone', '-b', 'main', str(self.origin), str(self.app))
        (self.source / 'requirements.txt').write_text('# version two\n')
        self.git(self.source, 'commit', '-am', 'two')
        self.new = self.git(self.source, 'rev-parse', 'HEAD')
        self.git(self.source, 'push', 'origin', 'main')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'calls'
        self.env = dict(os.environ, EASY_TEL_APP_DIR=str(self.app),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        DEPLOY_TEST_LOG=str(self.log))
        (self.app / '.venv' / 'bin').mkdir(parents=True)
        self.executable(self.bin / 'sudo', '''#!/bin/bash
echo "service:${@: -1}" >> "$DEPLOY_TEST_LOG"
''')
        self.executable(self.app / '.venv' / 'bin' / 'python', '''#!/bin/bash
echo "pip:$*" >> "$DEPLOY_TEST_LOG"
if [[ "$*" == *'pip install'* && "${FAIL_INSTALL:-0}" == 1 ]]; then exit 19; fi
''')

    @staticmethod
    def git(cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, check=True, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    @staticmethod
    def executable(path, text):
        path.write_text(text)
        path.chmod(0o755)

    def run_deploy(self, *args):
        return subprocess.run(['bash', str(SCRIPT), *args], env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_success_downloads_before_stop_and_checks_after_start(self):
        result = self.run_deploy(self.new)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.app, 'rev-parse', 'HEAD'), self.new)
        calls = self.log.read_text()
        self.assertLess(calls.index('pip wheel'), calls.index('service:stop'))
        self.assertLess(calls.index('service:stop'), calls.index('pip install'))
        self.assertLess(calls.index('pip check'), calls.index('service:start'))
        self.assertLess(calls.index('service:start'), calls.index('service:check'))

    def test_default_deploys_main_and_preserves_ignored_config(self):
        self.git(self.app, 'config', 'user.name', 'Deploy test')
        self.git(self.app, 'config', 'user.email', 'test@example.invalid')
        with (self.app / '.git' / 'info' / 'exclude').open('a') as file:
            file.write('\n.env\nstate/\n')
        (self.app / '.env').write_text('PRIVATE_LOCAL_SETTING=example\n')
        (self.app / 'state').mkdir()
        (self.app / 'state' / 'persistent').write_text('keep me')
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.app / '.env').read_text(), 'PRIVATE_LOCAL_SETTING=example\n')
        self.assertEqual((self.app / 'state' / 'persistent').read_text(), 'keep me')

    def test_rejects_local_edits_without_stopping(self):
        (self.app / 'requirements.txt').write_text('local edit\n')
        result = self.run_deploy(self.new)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Tracked local edits', result.stderr)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.git(self.app, 'rev-parse', 'HEAD'), self.old)

    def test_rejects_stale_commit_without_rewinding(self):
        self.git(self.app, 'pull', '--ff-only', 'origin', 'main')
        result = self.run_deploy(self.old)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to rewind', result.stderr)
        self.assertNotIn('service:stop', self.log.read_text())
        self.assertEqual(self.git(self.app, 'rev-parse', 'HEAD'), self.new)

    def test_failed_install_restarts_but_reports_failure(self):
        self.env['FAIL_INSTALL'] = '1'
        result = self.run_deploy(self.new)
        self.assertEqual(result.returncode, 19)
        calls = self.log.read_text()
        self.assertIn('service:stop', calls)
        self.assertIn('service:start', calls)
        self.assertNotIn('service:check', calls)
        self.assertIn('Deployment failed', result.stderr)

    def test_lock_prevents_overlapping_deployments(self):
        import fcntl
        with (self.app / '.deploy.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.run_deploy(self.new)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Another deployment', result.stderr)
        self.assertFalse(self.log.exists())
