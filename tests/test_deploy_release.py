import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_manifest import build_manifest
from scripts.deploy_release import (
    DeployError, active_counts, database_host_path, deploy, forbid_code_mounts,
    wait_for_idle, COUNT_SOURCE, BACKUP_SOURCE, backup_database, container_active_counts,
)


REVISION = "a" * 40
IMAGE = "ghcr.io/btcfoxman/pip2api@sha256:" + "b" * 64
OLD_ID = "sha256:" + "c" * 64


class DeploymentFixture:
    def __init__(self, root):
        self.root, self.commands = root, []
        (root / "data").mkdir()
        (root / "tmp").mkdir()
        self.database = root / "data/pip2api.db"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT, freebeat_serial_no TEXT)")
            db.execute("INSERT INTO tasks VALUES('historic','failed','')")
        self.layers = [root / name for name in ("docker-compose.yml", "cost/core.yml", "cost/table.json",
                                               "music/rollback.json", "music/latest.json")]
        for path in self.layers:
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"services": {"pip2api": {"image": "local/old:release"}}}))
        (root / ".env").write_text("PIP_API_KEY=PRIVATE_FIXTURE_VALUE\n")
        labels = {
            "com.docker.compose.project.working_dir": str(root),
            "com.docker.compose.project.config_files": ",".join(map(str, self.layers)),
            "com.docker.compose.project": "pip2api",
            "com.docker.compose.service": "pip2api",
            "org.opencontainers.image.revision": "d" * 40,
        }
        self.original = {
            "Id": "old-container", "Image": OLD_ID, "State": {"Running": True},
            "Config": {"Image": "local/old:release", "WorkingDir": "/app", "Labels": labels,
                       "Env": ["PIP_DB_PATH=data/pip2api.db", "PIP_API_KEY=PRIVATE_FIXTURE_VALUE"]},
            "Mounts": [
                {"Type": "bind", "Source": str(root / "data"), "Destination": "/app/data", "RW": True},
                {"Type": "bind", "Source": str(root / "tmp"), "Destination": "/app/tmp", "RW": True},
                {"Type": "bind", "Source": "/usr/local/bin/ffmpeg", "Destination": "/usr/local/bin/ffmpeg", "RW": False},
            ],
        }
        self.current = copy.deepcopy(self.original)
        self.config_mutation = False
        self.bad_revision = False
        self.change_env_file_after_pull = False
        self.accept_on_restart = False
        self.fail_new_health = False
        self.fail_manifest = False

    def task(self, ident, status, serial=""):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO tasks VALUES(?,?,?)", (ident, status, serial))

    def run(self, args, **kwargs):
        self.commands.append(list(args))
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([self.current])
        if args[:3] == ["docker", "image", "inspect"]:
            revision = "f" * 40 if self.bad_revision else REVISION
            return json.dumps([{"Config": {"Labels": {"org.opencontainers.image.revision": revision}}}])
        if args[:2] == ["docker", "pull"]:
            if self.change_env_file_after_pull:
                (self.root / ".env").write_text("PIP_API_KEY=CHANGED_PRIVATELY\n")
            return ""
        if args[:2] == ["docker", "compose"]:
            files = [Path(args[i+1]) for i, arg in enumerate(args) if arg == "-f"]
            image = "local/old:release"
            for path in files:
                image = json.loads(path.read_text())["services"]["pip2api"]["image"]
            if "config" in args:
                service = {"image": image, "environment": {"PIP_API_KEY": "PRIVATE_FIXTURE_VALUE"},
                           "volumes": [{"type": "bind", "source": str(self.root / "data"), "target": "/app/data"}]}
                if self.config_mutation and len(files) > 5:
                    service["environment"]["PIP_API_KEY"] = "UNEXPECTED_CHANGE"
                return json.dumps({"services": {"pip2api": service}})
            if "up" in args:
                self.current = copy.deepcopy(self.original)
                self.current["Id"] = "new-container" if image == IMAGE else "rollback-container"
                self.current["Config"]["Image"] = image
                if image == IMAGE:
                    self.current["Image"] = "sha256:" + "e" * 64
                    self.current["Config"]["Labels"]["org.opencontainers.image.revision"] = REVISION
                    if self.accept_on_restart:
                        self.task("newly-accepted", "submitted", "accepted-upstream-id")
                return ""
        if args[:2] == ["docker", "exec"]:
            if args[5] == COUNT_SOURCE:
                return json.dumps(active_counts(self.database))
            if args[5] == BACKUP_SOURCE:
                with closing(sqlite3.connect(self.database)) as source:
                    with closing(sqlite3.connect(self.root / "private-container-backup.db")) as target:
                        source.backup(target)
                return "/tmp/pip2api-release-fixture.db\n"
            if "unlink(missing_ok=True)" in args[5]:
                return ""
            if self.fail_manifest:
                raise DeployError("source manifest mismatch")
            return "source_manifest_verified"
        if args[:2] == ["docker", "cp"]:
            shutil.copyfile(self.root / "private-container-backup.db", args[3])
            return ""
        if args[:2] == ["docker", "login"]:
            return ""
        raise AssertionError("unexpected deployment operation: " + repr(args[:2]))

    def health(self, container, runner):
        if self.fail_new_health and self.current["Config"]["Image"] == IMAGE:
            raise DeployError("new release unhealthy")

    def perform(self, **kwargs):
        with patch.dict(os.environ, {}, clear=True):
            return deploy(deploy_path=self.root, service="pip2api", container="pip2api",
                          image=IMAGE, revision=REVISION, runner=self.run,
                          idle_wait=kwargs.pop("idle_wait", lambda read, timeout: None),
                          health_wait=self.health, **kwargs)

    def restarts(self):
        return [command for command in self.commands if command[:2] == ["docker", "compose"] and "up" in command]


class GuardedDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = DeploymentFixture(Path(self.directory.name).resolve())

    def test_full_release_retains_all_five_layers_environment_mounts_and_backup(self):
        fixture = self.fixture
        original = {path: path.read_bytes() for path in fixture.layers + [fixture.root / ".env"]}
        result = fixture.perform()
        self.assertEqual(result["compose_layers"], list(map(str, fixture.layers)))
        self.assertTrue(result["source_manifest_verified"])
        command = fixture.restarts()[0]
        files = [command[index+1] for index, value in enumerate(command) if value == "-f"]
        self.assertEqual(files[:5], list(map(str, fixture.layers)))
        self.assertEqual(len(files), 6)
        self.assertIn("--no-build", command)
        self.assertEqual(command[command.index("--pull")+1], "never")
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content)
        backups = list((fixture.root / ".workflow-rollouts").glob("*/database.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db, db:
            self.assertEqual(db.execute("SELECT id,status FROM tasks").fetchall(), [("historic", "failed")])
        self.assertNotIn("PRIVATE_FIXTURE_VALUE", json.dumps(result))

    def test_accepted_task_that_does_not_drain_prevents_any_restart_or_task_mutation(self):
        fixture = self.fixture
        fixture.task("accepted", "submitted", "original-upstream-id")
        clock = iter((0, 0, 1, 2))
        def short_wait(read, timeout):
            wait_for_idle(read, 1, clock=lambda: next(clock), sleep=lambda delay: None)
        with self.assertRaisesRegex(DeployError, "drain naturally"):
            fixture.perform(idle_wait=short_wait)
        self.assertEqual(fixture.restarts(), [])
        with closing(sqlite3.connect(fixture.database)) as db, db:
            self.assertEqual(db.execute("SELECT status,freebeat_serial_no FROM tasks WHERE id='accepted'").fetchone(),
                             ("submitted", "original-upstream-id"))

    def test_new_task_after_idle_samples_aborts_before_restart(self):
        fixture = self.fixture
        def arrival(read, timeout):
            self.assertEqual(sum(read().values()), 0)
            fixture.task("arrival", "queued")
        with self.assertRaisesRegex(DeployError, "new task arrived"):
            fixture.perform(idle_wait=arrival)
        self.assertEqual(fixture.restarts(), [])

    def test_unhealthy_new_release_rolls_back_to_original_immutable_id(self):
        fixture = self.fixture
        fixture.fail_new_health = True
        with self.assertRaisesRegex(DeployError, "previous immutable image restored"):
            fixture.perform()
        self.assertEqual(len(fixture.restarts()), 2)
        self.assertEqual(fixture.current["Image"], OLD_ID)
        self.assertEqual(fixture.current["Config"]["Image"], OLD_ID)

    def test_rollback_is_withheld_if_new_release_has_an_accepted_task(self):
        fixture = self.fixture
        fixture.fail_new_health, fixture.accept_on_restart = True, True
        with self.assertRaisesRegex(DeployError, "rollback withheld"):
            fixture.perform()
        self.assertEqual(len(fixture.restarts()), 1)
        self.assertEqual(active_counts(fixture.database), {"submitted": 1})

    def test_new_code_bind_is_rejected_before_pull_or_restart(self):
        fixture = self.fixture
        fixture.current["Mounts"].append({"Type": "bind", "Source": "/old/source", "Destination": "/app/app", "RW": False})
        with self.assertRaisesRegex(DeployError, "shadow"):
            fixture.perform()
        self.assertFalse(any(command[:2] == ["docker", "pull"] for command in fixture.commands))
        self.assertEqual(fixture.restarts(), [])

    def test_environment_change_in_candidate_compose_is_rejected(self):
        fixture = self.fixture
        fixture.config_mutation = True
        with self.assertRaisesRegex(DeployError, "more than the service image"):
            fixture.perform()
        self.assertEqual(fixture.restarts(), [])

    def test_external_config_change_during_pull_aborts_before_restart(self):
        fixture = self.fixture
        fixture.change_env_file_after_pull = True
        with self.assertRaisesRegex(DeployError, "configuration changed"):
            fixture.perform()
        self.assertEqual(fixture.restarts(), [])

    def test_wrong_published_revision_is_rejected_before_restart(self):
        fixture = self.fixture
        fixture.bad_revision = True
        with self.assertRaisesRegex(DeployError, "OCI revision mismatch"):
            fixture.perform()
        self.assertEqual(fixture.restarts(), [])

    def test_baked_source_manifest_mismatch_rolls_back_when_idle(self):
        fixture = self.fixture
        fixture.fail_manifest = True
        with self.assertRaisesRegex(DeployError, "previous immutable image restored"):
            fixture.perform()
        self.assertEqual(len(fixture.restarts()), 2)

    def test_partial_live_compose_layout_is_not_silently_replaced_with_base(self):
        fixture = self.fixture
        fixture.layers[-1].unlink()
        with self.assertRaisesRegex(DeployError, "all original live Compose layers"):
            fixture.perform()
        self.assertEqual(fixture.restarts(), [])

    def test_mutable_image_is_rejected(self):
        fixture = self.fixture
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(DeployError, "immutable GHCR digest"):
            deploy(deploy_path=fixture.root, service="pip2api", container="pip2api",
                   image="ghcr.io/btcfoxman/pip2api:latest", revision=REVISION, runner=fixture.run)
        self.assertEqual(fixture.commands, [])

    def test_no_remote_build_install_or_application_test_operation(self):
        fixture = self.fixture
        fixture.perform()
        for command in fixture.commands:
            self.assertNotIn("build", command)
            self.assertNotIn("pip", command)
            self.assertNotIn("pytest", command)
            self.assertNotIn("compileall", command)
        self.assertTrue(any(command[:2] == ["docker", "pull"] for command in fixture.commands))


class DeploymentHelpersTests(unittest.TestCase):
    def test_container_counts_are_readonly_and_invalid_counts_fail_closed(self):
        commands = []
        def response(args):
            commands.append(args)
            return '{"submitted": 1, "account_logging_in": 1}'
        self.assertEqual(container_active_counts('pip2api', response), {"submitted": 1, "account_logging_in": 1})
        self.assertIn("?mode=ro", commands[0][-1])
        self.assertIn("PRAGMA query_only=ON", commands[0][-1])
        self.assertNotIn("app.", commands[0][-1])
        for invalid in ('[]', '{"running":-1}', '{"running":true}'):
            with self.subTest(invalid=invalid), self.assertRaises(DeployError):
                container_active_counts('pip2api', lambda args: invalid)

    def test_container_backup_copy_failure_still_removes_only_its_private_file(self):
        commands = []
        def runner(args):
            commands.append(args)
            if args[-1] == BACKUP_SOURCE:
                return '/tmp/pip2api-release-fixture.db'
            if args[:2] == ['docker', 'cp']:
                raise DeployError('synthetic copy failure')
            return ''
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(DeployError):
            backup_database('pip2api', Path(directory) / 'db.sqlite3', runner)
        self.assertIn("Path('/tmp/pip2api-release-fixture.db').unlink(missing_ok=True)", commands[-1][-1])
        self.assertIn("?mode=ro", commands[0][-1])

    def test_zero_samples_are_required_to_be_consecutive(self):
        counts = iter(({}, {"preparing": 1}, {}, {}, {}))
        calls = []
        wait_for_idle(lambda: calls.append(1) or next(counts), 100, clock=lambda: 0, sleep=lambda delay: None)
        self.assertEqual(len(calls), 5)

    def test_all_live_task_phases_block_idle(self):
        for phase in ("queued", "preparing", "submitted", "running", "polling", "postprocessing"):
            with self.subTest(phase=phase):
                with self.assertRaises(DeployError):
                    wait_for_idle(lambda: {phase: 1}, 0, clock=lambda: 0, sleep=lambda delay: None)

    def test_shadowing_ancestor_file_and_module_mounts_are_blocked(self):
        for target in ("/", "/app", "/app/app", "/app/main.py", "/app/app/service.py", "/app/requirements.txt"):
            with self.subTest(target=target), self.assertRaises(DeployError):
                forbid_code_mounts([{"target": target}])
        forbid_code_mounts([{"target": "/app/data"}, {"target": "/app/tmp"}, {"target": "/usr/local/bin/ffmpeg"}])

    def test_missing_or_unpersisted_database_is_rejected(self):
        container = {"Config": {"Env": [], "WorkingDir": "/app"}, "Mounts": []}
        with self.assertRaisesRegex(DeployError, "persisted"):
            database_host_path(container)

    def test_manifest_covers_source_assets_entrypoint_and_dependencies_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app/static").mkdir(parents=True)
            (root / "app/__pycache__").mkdir()
            for name in ("app/main.py", "app/cost_reference.py", "app/static/app.js", "app/cost_reference_seed.json", "main.py", "requirements.txt"):
                (root / name).write_text(name)
            (root / "app/__pycache__/old.pyc").write_text("bytecode")
            (root / "app/deployment_manifest.json").write_text("old-manifest")
            result = build_manifest(root, REVISION)
            self.assertEqual(result["revision"], REVISION)
            self.assertEqual(len(result["files"]), 5)
            self.assertNotIn("app/deployment_manifest.json", result["files"])
            self.assertFalse(any("__pycache__" in path for path in result["files"]))

    def test_manifest_rejects_abbreviated_revision(self):
        with self.assertRaises(ValueError):
            build_manifest(Path("."), "abcdef123456")
