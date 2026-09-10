import subprocess


def git(path, *args):
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=",
            "-c",
            "user.name=Redesign Test",
            "-c",
            "user.email=redesign@example.invalid",
            "-C",
            str(path),
            *args,
        ],
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout


def create_repository(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    (path / "tracked.txt").write_text("original\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-m", "fixture")
    return path
