"""Approved video cleanup. Dry-run unless --apply; Python stdlib only.

Stop renderers and other writers before --apply. Cache commands affect user-wide caches.
"""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch
import json
import ntpath
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time


class UnsafePath(ValueError):
    pass


def linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400)


def inside(path, root):
    return path != root and path.is_relative_to(root)


def checked(path, root):
    """Validate lexical AND resolved containment, and every existing component."""
    if not inside(path, root) or not inside(path.resolve(), root.resolve()):
        raise UnsafePath(f"Rejected path outside the root: {path}")
    current = root
    for component in (".", *path.relative_to(root).parts):
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if linked(info):
            raise UnsafePath(f"Rejected operation through a link/junction: {current}")
    return path


def project_root(value):
    root = Path(os.path.abspath(value))
    for part in [*reversed(root.parents), root]:
        if linked(part.lstat()):
            raise UnsafePath(f"Link/junction in the project root: {part}")
    if not root.is_dir() or root == Path(root.anchor):
        raise UnsafePath(f"Invalid project folder: {root}")
    return root


@dataclass
class Entry:
    path: Path
    info: os.stat_result
    children: list
    delete: bool = False
    size: int = 0
    files: int = 0


def scan(root):
    def visit(path):
        info = path.lstat()
        item = Entry(path, info, [])
        if linked(info):
            if not inside(path.resolve(), root.resolve()):
                raise UnsafePath(f"Rejected outward-pointing link/junction: {path}")
            return item
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                names = sorted(entry.name for entry in entries)
            item.children = [visit(path / name) for name in names]
            item.size = sum(child.size for child in item.children)
            item.files = sum(child.files for child in item.children)
        else:
            item.size = info.st_size
            item.files = 1
        return item
    return visit(root)


def flatten(item):
    yield item
    for child in item.children:
        yield from flatten(child)


def pattern_parts(value):
    if not isinstance(value, str) or not value or ntpath.splitdrive(value)[0]:
        raise UnsafePath(f"Invalid deliverable path: {value!r}")
    value = value.replace("\\", "/")
    parts = value.split("/")
    if value.startswith("/") or any(
        p in ("", ".", "..") or ":" in p or p.endswith((" ", ".")) for p in parts
    ):
        raise UnsafePath(f"Rejected absolute/traversing deliverable path: {value!r}")
    return tuple(p.casefold() for p in parts)


def matches(parts, pattern):
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return matches(parts, pattern[1:]) or bool(parts and matches(parts[1:], pattern))
    return bool(parts and fnmatch.fnmatchcase(parts[0], pattern[0])
                and matches(parts[1:], pattern[1:]))


def deliveries(root, tree, cli):
    manifest = checked(root / "edit" / "deliverables.json", root)
    values = []
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("keep"), list) or not data["keep"]:
            raise ValueError("deliverables.json: a non-empty keep list is required")
        values.extend(data["keep"])
    elif not cli:
        raise ValueError("edit/deliverables.json missing; --keep-renders is required")
    for value in cli or []:
        parts = pattern_parts(value)
        values.append(value if parts[0] == "renders" else "renders/" + value)
    items = list(flatten(tree))[1:]
    keep = set()
    for value in values:
        pattern = pattern_parts(value)
        found = [item.path for item in items if matches(
            tuple(p.casefold() for p in item.path.relative_to(root).parts), pattern)]
        if not found:
            raise ValueError(f"Deliverable pattern matched no file: {value}")
        keep.update(found)
    return keep


def deletion_candidate(relative, is_dir, size):
    parts = tuple(p.casefold() for p in relative.parts)
    if ".git" in parts or parts[-1] == ".gitignore" or relative.suffix.casefold() == ".srt":
        return False
    if parts[0] in {"raw", "scripts", "edit", "docs", "qa", ".venv"}:
        return False
    if parts[:2] == ("work", "models"):
        return False
    cache_dirs = {"shorts", "segments", "composite-v1", "shorts-test",
                  "render-tests", "sync-check", "frames"}
    if (parts[0] == "work" and not is_dir and size <= 10_000_000 and
        relative.suffix.casefold() in {".json", ".md", ".txt", ".srt", ".csv", ".ass"} and
        not (len(parts) > 2 and parts[1] in cache_dirs)):
        return False
    return (parts[0] in {"work", "overlays", "renders"} or
            (parts[0] == "hf" and any(p in {"renders", "node_modules"}
                for p in (parts[1:] if is_dir else parts[1:-1]))))


def classify(item, root, keep):
    for child in item.children:
        classify(child, root, keep)
    if item.path == root:
        return
    protected = any(p in keep for p in [item.path, *item.path.parents])
    item.delete = (not protected and not linked(item.info) and
                   deletion_candidate(item.path.relative_to(root), stat.S_ISDIR(item.info.st_mode), item.size) and
                   all(child.delete for child in item.children))


def rows(item, root):
    statuses = {child.delete for child in flatten(item)}
    if item.path != root and len(statuses) == 1:
        label = item.path.relative_to(root).as_posix()
        if stat.S_ISDIR(item.info.st_mode):
            label += "/"
        if linked(item.info):
            label += " [link/junction; takip edilmedi]"
        yield item.delete, label, item.size, item.files
    else:
        for child in item.children:
            yield from rows(child, root)


def fingerprint(info):
    return info.st_dev, info.st_ino, info.st_mode


def unchanged(item, root):
    if linked(item.info):
        if not inside(item.path.resolve(), root.resolve()):
            raise UnsafePath(f"Outward-pointing link/junction: {item.path}")
    else:
        checked(item.path, root)
    now = item.path.lstat()
    if fingerprint(now) != fingerprint(item.info) or (
        (linked(now) or not stat.S_ISDIR(now.st_mode)) and
        (now.st_size, now.st_mtime_ns) != (item.info.st_size, item.info.st_mtime_ns)
    ):
        raise UnsafePath(f"Path changed after scan: {item.path}")


def command(args):
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", NO_UPDATE_NOTIFIER="1")
    result = subprocess.run(args, capture_output=True, text=True, check=True, env=env, timeout=60)
    return result.stdout.strip()


def cache_plan(root):
    jobs = []
    python = getattr(sys, "_base_executable", sys.executable)
    commands = [("pip", [python, "-m", "pip", "cache", "dir"],
                 [python, "-m", "pip", "cache", "purge"])]
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm:
        commands.append(("npm", [npm, "config", "get", "cache"],
                         [npm, "cache", "clean", "--force"]))
    else:
        print("WARNING: npm not found; npm cache skipped")
    for name, query, purge in commands:
        location = Path(command(query))
        if not location.is_absolute():
            raise UnsafePath(f"Invalid cache path: {location}")
        location = project_root(location) if location.exists() else location
        # Cache commands must never reach project data or a broad user/root directory.
        if (location == Path(location.anchor) or location == Path.home() or
            location.is_relative_to(root) or root.is_relative_to(location)):
            raise UnsafePath(f"Unsafe cache scope: {location}")
        purge = [*purge, "--cache-dir" if name == "pip" else "--cache", str(location)]
        tree = scan(location) if location.exists() else None
        if tree and any(linked(x.info) for x in flatten(tree)):
            raise UnsafePath(f"Link/junction inside cache: {location}")
        jobs.append((name, location, tree, purge, False))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        temp = Path(local) / "Temp"
        if temp.exists():
            temp = project_root(temp)
            for candidate in sorted(temp.iterdir()):
                if not any(candidate.name.startswith(prefix) for prefix in
                           ("hyperframes-extract-cache-", "ad-chrome-")):
                    continue
                checked(candidate, temp)
                if candidate.is_relative_to(root) or root.is_relative_to(candidate):
                    raise UnsafePath(f"Temp cache overlaps the project: {candidate}")
                if not candidate.is_dir():
                    continue
                tree = scan(candidate)
                if any(linked(x.info) for x in flatten(tree)):
                    raise UnsafePath(f"Link/junction inside temp cache: {candidate}")
                old = tree.info.st_mtime < time.time() - 3600
                jobs.append(("temp", candidate, tree, None, not old))
    return jobs


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def save_log(root, data):
    edit = checked(root / "edit", root)
    edit.mkdir(exist_ok=True)
    log = checked(edit / "cleanup-log.json", root)
    descriptor, name = tempfile.mkstemp(prefix=".cleanup-log-", suffix=".json", dir=edit)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        checked(log, root)
        os.replace(temporary, log)
    finally:
        if temporary.exists():
            checked(temporary, root).unlink()


def remove(items, boundary, records, label_root):
    for item in sorted(items, key=lambda x: len(x.path.parts), reverse=True):
        unchanged(item, boundary)
        if stat.S_ISDIR(item.info.st_mode):
            item.path.rmdir()
        else:
            item.path.unlink()
        records.append({"path": str(item.path.relative_to(label_root)) if
                        item.path.is_relative_to(label_root) else str(item.path),
                        "size_bytes": 0 if stat.S_ISDIR(item.info.st_mode) else item.size,
                        "date": timestamp()})


def apply(root, tree, jobs):
    doomed = [x for x in flatten(tree) if x.delete]
    for item in list(flatten(tree))[1:]:
        unchanged(item, root)
    for _, location, cached, _, skip in jobs:
        if not skip and cached:
            for item in flatten(cached):
                unchanged(item, location.parent)
    log = checked(root / "edit" / "cleanup-log.json", root)
    history = json.loads(log.read_text(encoding="utf-8")) if log.exists() else {"runs": []}
    if not isinstance(history, dict) or not isinstance(history.get("runs"), list):
        raise ValueError("Existing cleanup-log.json is invalid; not overwritten")
    run = {"date": timestamp(), "deleted": [], "cache_commands": [], "status": "started"}
    history["runs"].append(run)
    save_log(root, history)
    try:
        remove(doomed, root, run["deleted"], root)
        for name, location, cached, purge, skip in jobs:
            if skip or cached is None:
                continue
            if name == "temp":
                if location.stat().st_mtime >= time.time() - 3600:
                    raise UnsafePath(f"Temp cache is no longer older than 1 hour: {location}")
                remove(list(flatten(cached)), location.parent, run["deleted"], root)
            else:
                # Cache command sizes are estimates: pip/npm retain some cache metadata.
                for item in flatten(cached):
                    unchanged(item, location.parent)
                try:
                    output = command(purge)
                finally:
                    for item in flatten(cached):
                        if not os.path.lexists(item.path):
                            run["deleted"].append({"path": str(item.path),
                                "size_bytes": 0 if stat.S_ISDIR(item.info.st_mode) else item.size,
                                "date": timestamp()})
                run["cache_commands"].append({"command": purge, "path": str(location),
                    "estimated_size_bytes": cached.size, "date": timestamp(), "output": output})
        run["status"] = "complete"
    except BaseException as error:
        run["status"] = "failed"
        run["error"] = str(error)
        raise
    finally:
        save_log(root, history)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--keep-renders", nargs="+")
    parser.add_argument("--purge-caches", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = project_root(args.project)
        tree = scan(root)
        keep = deliveries(root, tree, args.keep_renders)
        classify(tree, root, keep)
        jobs = cache_plan(root) if args.purge_caches else []
        print(f"{'APPLY' if args.apply else 'DRY-RUN'}: {root}")
        print(f"{'STATUS':<12} {'SIZE (GB)':>12} {'FILES':>9}  PATH")
        counts = {True: [0, 0, 0], False: [0, 0, 0]}
        for delete, label, size, files in rows(tree, root):
            status = 'DELETE' if delete else 'KEEP'
            print(f"{status:<12} {size / 1e9:12.6f} {files:9d}  {label}")
            counts[delete][0] += size
            counts[delete][1] += files
            counts[delete][2] += 1
        for delete, title in [(True, "Delete"), (False, "Keep")]:
            size, files, count = counts[delete]
            print(f"{title}: {size / 1e9:.6f} GB | {files} files | {count} rows")
        estimate = 0
        for name, location, cached, purge, skip in jobs:
            size = cached.size if cached else 0
            status = "KEEP" if skip else "DELETE"
            suffix = " (<1 hour)" if skip else " (estimate)" if purge else ""
            print(f"{status:<12} {size / 1e9:12.6f} {cached.files if cached else 0:9d}  {name}: {location}{suffix}")
            if not skip:
                estimate += size
        print(f"Total freed: {counts[True][0] / 1e9:.6f} GB" +
              (f" + up to about {estimate / 1e9:.6f} GB cache" if jobs else ""))
        print("Sizes are logical bytes, GB = 1,000,000,000 bytes; link targets are not counted.")
        if args.apply:
            apply(root, tree, jobs)
            print("Done. Log: edit/cleanup-log.json")
        else:
            print("Dry-run: nothing deleted, no log written.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
