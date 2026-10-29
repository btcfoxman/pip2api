"""Pull/restart a verified image while preserving the actual live Compose layers.

Run from Actions over SSH. Standard library only: no application imports, package
installation, image builds or full-application verification on the server.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Callable


ACTIVE_STATUSES = ("queued", "preparing", "submitted", "running", "polling", "postprocessing", "submitting")
MANIFEST_CHECK = """
import hashlib,json,pathlib,sys
root=pathlib.Path('/app')
m=json.loads((root/'app/deployment_manifest.json').read_text())
assert m['revision']==sys.argv[1], 'manifest revision mismatch'
assert m['files'], 'empty source manifest'
for name, expected in m['files'].items():
    p=(root/name).resolve()
    assert p.is_relative_to(root), 'invalid manifest path'
    assert hashlib.sha256(p.read_bytes()).hexdigest()==expected, 'source hash mismatch'
print('source_manifest_verified')
"""


class DeployError(RuntimeError):
    pass


def run(args: list[str], *, input_text: str | None = None, timeout: int = 60) -> str:
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        # Docker/Compose may print resolved credentials. Keep their output private.
        raise DeployError("command failed: " + " ".join(args[:2]))
    return result.stdout


def inspect_container(container: str, runner=run) -> dict:
    items = json.loads(runner(["docker", "inspect", container]))
    if len(items) != 1:
        raise DeployError("expected exactly one existing container")
    return items[0]


def forbid_code_mounts(mounts: list[dict]) -> None:
    for mount in mounts:
        target = str(mount.get("Destination") or mount.get("target") or "").rstrip("/") or "/"
        # Data and temporary assets stay mounted; source must come from the image.
        if target == "/" or target == "/app" or (
            target.startswith("/app/") and not any(
                target == allowed or target.startswith(allowed + "/")
                for allowed in ("/app/data", "/app/tmp")
            )
        ):
            raise DeployError("an application code/config mount would shadow the release image")


def application_environment(container: dict) -> dict[str, str]:
    values = dict(item.split("=", 1) for item in container["Config"].get("Env", []) if "=" in item)
    return {key: value for key, value in values.items()
            if key.startswith("PIP_") or key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}


def mount_identity(container: dict) -> list[tuple]:
    return sorted((str(m.get("Type")), str(m.get("Source")), str(m.get("Destination")), bool(m.get("RW")))
                  for m in container.get("Mounts", []))


def database_host_path(container: dict) -> Path:
    env = application_environment(container)
    path = PurePosixPath(env.get("PIP_DB_PATH") or str(PurePosixPath(env.get("PIP_DATA_DIR") or "/app/data") / "pip2api.db"))
    if not path.is_absolute():
        path = PurePosixPath(container["Config"].get("WorkingDir") or "/app") / path
    for mount in sorted(container.get("Mounts", []), key=lambda m: len(m.get("Destination", "")), reverse=True):
        target = PurePosixPath(mount.get("Destination") or "/")
        if mount.get("Type") == "bind" and path.is_relative_to(target):
            result = (Path(mount["Source"]) / str(path.relative_to(target))).resolve()
            if not result.is_relative_to(Path(mount["Source"]).resolve()) or not result.is_file():
                raise DeployError("database bind path is missing or escapes its mount")
            return result
    raise DeployError("database must be persisted in an existing bind mount")


def active_counts(database: Path) -> dict[str, int]:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        rows = db.execute(f"SELECT status, COUNT(*) FROM tasks WHERE status IN ({placeholders}) GROUP BY status", ACTIVE_STATUSES)
        result = {str(status): int(count) for status, count in rows}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
            for status, count in db.execute("SELECT status,COUNT(*) FROM accounts WHERE status IN ('logging_in','login_pending','authorizing','verifying') GROUP BY status"):
                result["account_" + str(status)] = int(count)
        return result


COUNT_SOURCE = """
import json,os,sqlite3
from pathlib import Path
path=Path(os.environ.get('PIP_DATA_DIR') or '/app/data')/'pip2api.db'
with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5) as db:
    db.execute('PRAGMA query_only=ON')
    states=('queued','preparing','submitting','submitted','running','polling','postprocessing')
    rows=db.execute('SELECT status,COUNT(*) FROM tasks WHERE status IN ('+','.join('?' for _ in states)+') GROUP BY status',states)
    result={str(status):int(count) for status,count in rows}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
        for status,count in db.execute("SELECT status,COUNT(*) FROM accounts WHERE status IN ('logging_in','login_pending','authorizing','verifying') GROUP BY status"):
            result['account_'+str(status)]=int(count)
print(json.dumps(result))
"""
BACKUP_SOURCE = """
import os,sqlite3,tempfile
from pathlib import Path
path=Path(os.environ.get('PIP_DATA_DIR') or '/app/data')/'pip2api.db'
with tempfile.NamedTemporaryFile(prefix='pip2api-release-',suffix='.db',delete=False) as f:
    temporary=Path(f.name)
try:
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5) as source:
        source.execute('PRAGMA query_only=ON')
        with sqlite3.connect(temporary) as destination:
            source.backup(destination)
    print(str(temporary))
except BaseException:
    temporary.unlink(missing_ok=True)
    raise
"""


def container_active_counts(container: str, runner=run) -> dict[str, int]:
    # The runner must not need host access to root-owned application data.
    counts = json.loads(runner(["docker", "exec", container, "python", "-c", COUNT_SOURCE]))
    if not isinstance(counts, dict) or any(not isinstance(key, str) or type(value) is not int or value < 0 for key, value in counts.items()):
        raise DeployError("invalid read-only activity count")
    return counts


def backup_database(container: str, destination: Path, runner=run) -> None:
    temporary = runner(["docker", "exec", container, "python", "-c", BACKUP_SOURCE]).strip()
    path = PurePosixPath(temporary)
    if path.parent != PurePosixPath('/tmp') or not path.name.startswith('pip2api-release-') or not path.name.endswith('.db'):
        raise DeployError("invalid private database backup path")
    try:
        runner(["docker", "cp", container + ":" + temporary, str(destination)])
        destination.chmod(0o600)
    finally:
        runner(["docker", "exec", container, "python", "-c",
                "from pathlib import Path; Path(" + repr(temporary) + ").unlink(missing_ok=True)"])


def wait_for_idle(read_counts: Callable[[], dict], timeout: float, *, sleep=time.sleep,
                  clock=time.monotonic, stable_samples: int = 3) -> None:
    deadline, stable = clock() + timeout, 0
    while True:
        counts = read_counts()
        stable = stable + 1 if not sum(counts.values()) else 0
        if stable >= stable_samples:
            return
        if clock() >= deadline:
            raise DeployError("active tasks did not drain naturally; container was not restarted")
        sleep(2)


def _file_hashes(files: list[Path]) -> dict[str, str]:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def compose_layout(container: dict, deploy_path: Path, service: str) -> tuple[list[str], list[Path]]:
    labels = container["Config"].get("Labels") or {}
    working = Path(labels.get("com.docker.compose.project.working_dir") or "").resolve()
    if working != deploy_path.resolve() or labels.get("com.docker.compose.service") != service:
        raise DeployError("live Compose identity does not match deployment configuration")
    project = str(labels.get("com.docker.compose.project") or "")
    files = [Path(name) for name in str(labels.get("com.docker.compose.project.config_files") or "").split(",") if name]
    if not project or not files or any(not path.is_absolute() or not path.is_file() for path in files):
        raise DeployError("cannot preserve all original live Compose layers")
    prefix = ["docker", "compose", "--project-directory", str(working), "--project-name", project]
    for path in files:
        prefix.extend(["-f", str(path)])
    return prefix, files


def resolved_service(prefix: list[str], service: str, runner=run) -> dict:
    config = json.loads(runner(prefix + ["config", "--format", "json"]))
    result = config.get("services", {}).get(service)
    if not isinstance(result, dict) or not result.get("image") or "build" in result:
        raise DeployError("deployment requires an image-only Compose service")
    forbid_code_mounts(result.get("volumes", []))
    return result


def image_override(path: Path, service: str, image: str) -> None:
    path.write_text(json.dumps({"services": {service: {"image": image}}}, indent=2), encoding="utf-8")
    path.chmod(0o600)


def preserve_only_image(before: dict, after: dict) -> None:
    if {k: v for k, v in before.items() if k != "image"} != {k: v for k, v in after.items() if k != "image"}:
        raise DeployError("candidate Compose changes more than the service image")


def ensure_unchanged(before: dict, container: str, files: list[Path], hashes: dict, runner=run) -> None:
    now = inspect_container(container, runner)
    if now["Id"] != before["Id"] or now["Image"] != before["Image"] or _file_hashes(files) != hashes:
        raise DeployError("live container or configuration changed during deployment")


def verify_release(container: str, image: str, revision: str, before: dict, runner=run) -> None:
    current = inspect_container(container, runner)
    if not current.get("State", {}).get("Running") or current["Config"]["Image"] != image:
        raise DeployError("container is not running the requested immutable image")
    if (current["Config"].get("Labels") or {}).get("org.opencontainers.image.revision") != revision:
        raise DeployError("container revision does not match the requested commit")
    forbid_code_mounts(current.get("Mounts", []))
    if mount_identity(current) != mount_identity(before) or application_environment(current) != application_environment(before):
        raise DeployError("application mounts or environment changed")
    runner(["docker", "exec", container, "python", "-c", MANIFEST_CHECK, revision])


def healthy(container: str, runner=run) -> bool:
    try:
        runner(["docker", "exec", container, "python", "-c",
                "import os,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PIP_PORT','8807')+'/health',timeout=3); assert r.status==200"], timeout=8)
        return True
    except (DeployError, subprocess.TimeoutExpired):
        return False


def wait_for_health(container: str, runner=run, *, attempts=20, sleep=time.sleep) -> None:
    for _ in range(attempts):
        if healthy(container, runner):
            return
        sleep(2)
    raise DeployError("release failed the lightweight health check")


def deploy(*, deploy_path: Path, service: str, container: str, image: str, revision: str,
           idle_timeout: int = 600, runner=run, idle_wait=wait_for_idle, health_wait=wait_for_health) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DeployError("release revision must be a full Git SHA")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}", image):
        raise DeployError("release image must be an immutable GHCR digest")
    before = inspect_container(container, runner)
    if not before.get("State", {}).get("Running"):
        raise DeployError("existing service must be running before a guarded rollout")
    forbid_code_mounts(before.get("Mounts", []))
    prefix, layers = compose_layout(before, deploy_path, service)
    original_service = resolved_service(prefix, service, runner)
    database = database_host_path(before)
    preserved_files = layers + ([deploy_path / ".env"] if (deploy_path / ".env").is_file() else [])
    hashes = _file_hashes(preserved_files)
    release = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + revision[:12]
    rollout = deploy_path / ".workflow-rollouts" / release
    rollout.mkdir(parents=True, exist_ok=False, mode=0o700)
    rollout.chmod(0o700)
    for index, path in enumerate(preserved_files):
        copied = rollout / f"original-{index}-{path.name}"
        shutil.copyfile(path, copied)
        copied.chmod(0o600)
    candidate_override, rollback_override = rollout / "release.compose.json", rollout / "rollback.compose.json"
    image_override(candidate_override, service, image)
    image_override(rollback_override, service, before["Image"])
    candidate_prefix = prefix + ["-f", str(candidate_override)]
    rollback_prefix = prefix + ["-f", str(rollback_override)]
    preserve_only_image(original_service, resolved_service(candidate_prefix, service, runner))
    preserve_only_image(original_service, resolved_service(rollback_prefix, service, runner))
    # Registry credentials are accepted only through environment/stdin, never flags/logs.
    token = os.environ.get("GHCR_TOKEN")
    if token:
        username = os.environ.get("GHCR_USERNAME") or ""
        if not username:
            raise DeployError("GHCR_USERNAME is required when a registry token is set")
        runner(["docker", "login", "ghcr.io", "-u", username, "--password-stdin"], input_text=token, timeout=60)
    runner(["docker", "pull", image], timeout=600)
    published = json.loads(runner(["docker", "image", "inspect", image]))[0]
    if (published["Config"].get("Labels") or {}).get("org.opencontainers.image.revision") != revision:
        raise DeployError("published image OCI revision mismatch")
    read_counts = lambda: container_active_counts(container, runner)
    idle_wait(read_counts, idle_timeout)
    ensure_unchanged(before, container, preserved_files, hashes, runner)
    backup_database(container, rollout / "database.sqlite3", runner)
    # Last guard immediately before restart. Never cancel/fail/requeue live tasks.
    if sum(read_counts().values()):
        raise DeployError("a new task arrived before restart; existing service was preserved")
    restart = ["up", "-d", "--no-build", "--pull", "never", "--no-deps", service]
    try:
        runner(candidate_prefix + restart, timeout=180)
        health_wait(container, runner)
        verify_release(container, image, revision, before, runner)
    except Exception:
        # Do not interrupt a task accepted by the new release in order to roll back.
        try:
            safe = not sum(read_counts().values())
        except Exception:
            safe = False
        if not safe:
            raise DeployError("release verification failed; automatic rollback withheld because task safety is unknown") from None
        runner(rollback_prefix + restart, timeout=180)
        health_wait(container, runner)
        restored = inspect_container(container, runner)
        if restored["Image"] != before["Image"] or mount_identity(restored) != mount_identity(before):
            raise DeployError("rollback did not restore the original immutable image and mounts") from None
        if application_environment(restored) != application_environment(before):
            raise DeployError("rollback application environment mismatch") from None
        raise DeployError("release verification failed; previous immutable image restored") from None
    if _file_hashes(preserved_files) != hashes:
        raise DeployError("original Compose/environment files changed unexpectedly")
    proof = {"revision": revision, "image": image, "previous_image_id": before["Image"],
             "compose_layers": [str(path) for path in layers], "configuration_sha256": hashes,
             "mounts_preserved": True, "application_environment_preserved": True,
             "source_manifest_verified": True, "rollback_override": str(rollback_override)}
    (rollout / "result.safe.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy-path", type=Path, default=os.environ.get("DEPLOY_PATH"), required=not bool(os.environ.get("DEPLOY_PATH")))
    parser.add_argument("--service", default=os.environ.get("DEPLOY_SERVICE") or "pip2api")
    parser.add_argument("--container", default=os.environ.get("DEPLOY_CONTAINER") or "pip2api")
    parser.add_argument("--image", default=os.environ.get("RELEASE_IMAGE"), required=not bool(os.environ.get("RELEASE_IMAGE")))
    parser.add_argument("--revision", default=os.environ.get("RELEASE_REVISION"), required=not bool(os.environ.get("RELEASE_REVISION")))
    parser.add_argument("--idle-timeout", type=int, default=600)
    args = parser.parse_args()
    try:
        print(json.dumps(deploy(deploy_path=args.deploy_path, service=args.service, container=args.container,
                                image=args.image, revision=args.revision, idle_timeout=args.idle_timeout)))
    except (DeployError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise SystemExit("deployment stopped: " + str(exc)) from None
