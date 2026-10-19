"""Offline cleanup checks; --apply runs ONLY inside tempfile projects."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="video-cleanup-test-")
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "project"
        self.root.mkdir()
        self.kept = ["raw/clip.mp4", "scripts/render.py", "edit/cuts.json",
                     "docs/notes.md", "hf/scene/index.html", "hf/scene/renders.srt",
                     "hf/source/renders", "qa/board.jpg", "qa/sub/check.json",
                     ".venv/bin/python", "work/models/whisper.bin", ".git/config",
                     ".gitignore", "renders/final-v2.mp4", "renders/shorts/v2/a.mp4",
                     "renders/shorts/v2/a.srt", "work/nested/transcript.SRT",
                     "overlays/captions.srt", "hf/scene/node_modules/keep.srt",
                     "overlays/nested/.git/config", "other.txt"]
        self.deleted = ["work/clip.wav", "work/nested/frame.jpg", "overlays/title.mov",
                        "renders/base.mp4", "renders/final.mp4", "renders/v1-mobil.mp4",
                        "renders/shorts/a.mp4", "hf/scene/renders/frame.mp4",
                        "hf/scene/node_modules/package/code.js"]
        for name in self.kept + self.deleted:
            self.write(name)
        self.manifest(["renders/final-v2.mp4", "renders/shorts/v2/*.mp4",
                       "renders/shorts/v2/*.srt"])
        (self.root / "work/empty").mkdir()

    def write(self, name, content="fixture"):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def manifest(self, values):
        self.write("edit/deliverables.json", json.dumps({"keep": values}))

    def cli(self, *options):
        result = subprocess.run([sys.executable, "-B", str(Path(cleanup.__file__)),
                                 str(self.root), *options], capture_output=True,
                                encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        return result

    def plan(self):
        tree = cleanup.scan(self.root)
        keep = cleanup.deliveries(self.root, tree, None)
        cleanup.classify(tree, self.root, keep)
        return tree

    def test_dry_run_and_apply(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        dry = self.cli()
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("DELETE", dry.stdout)
        self.assertIn("KEEP", dry.stdout)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                 for p in self.root.rglob("*") if p.is_file()})
        tree = self.plan()
        self.assertEqual(sum(size for _, _, size, _ in cleanup.rows(tree, self.root)), tree.size)
        result = self.cli("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in self.kept:
            self.assertTrue((self.root / name).is_file(), name)
        for name in self.deleted + ["work/empty"]:
            self.assertFalse((self.root / name).exists(), name)
        log = json.loads((self.root / "edit/cleanup-log.json").read_text(encoding="utf-8"))
        run = log["runs"][0]
        self.assertEqual(run["status"], "complete")
        logged = {Path(item["path"]).as_posix(): item for item in run["deleted"]}
        for name in self.deleted:
            self.assertEqual(logged[name]["size_bytes"], len(b"fixture"))
            self.assertTrue(logged[name]["date"])
        self.assertEqual(self.cli("--apply").returncode, 0)
        self.assertEqual(len(json.loads((self.root / "edit/cleanup-log.json").read_text())["runs"]), 2)

    def test_small_work_analysis_sources(self):
        kept = ["work/transcript.json", "work/sync.json", "work/edl.json",
                "work/edit-review.md", "work/music-bed.json", "work/notes.TXT",
                "work/decisions.csv", "work/subtitles.ass", "work/limit.json",
                "work/analysis/nested/decisions.json", "work/analysis/readme.md"]
        deleted = ["work/oversize.json", "work/temp.py", "work/render.log"]
        for directory in ["shorts", "segments", "composite-v1", "shorts-test",
                          "render-tests", "sync-check", "frames"]:
            deleted.extend(f"work/{directory}/nested/decision.{extension}"
                           for extension in ["json", "md", "txt", "csv", "ass"])
        for name in kept + deleted:
            target = self.write(name)
            if name in ["work/limit.json", "work/oversize.json"]:
                with target.open("r+b") as handle:
                    handle.truncate(10_000_000 + (name == "work/oversize.json"))
        dry = self.cli()
        self.assertEqual(dry.returncode, 0, dry.stderr)
        for name in kept:
            if len(Path(name).parts) == 2:
                self.assertTrue(any(line.startswith("KEEP") and line.endswith("  " + name)
                                    for line in dry.stdout.splitlines()), name)
        planned = {item.path.relative_to(self.root).as_posix(): item.delete
                   for item in cleanup.flatten(self.plan())}
        for name in kept:
            self.assertFalse(planned[name], name)
        for name in deleted:
            self.assertTrue(planned[name], name)
        result = self.cli("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in kept:
            self.assertTrue((self.root / name).is_file(), name)
        for name in deleted:
            self.assertFalse((self.root / name).exists(), name)

    def test_manifest_required_and_cli_fallback(self):
        (self.root / "edit/deliverables.json").unlink()
        self.assertNotEqual(self.cli("--apply").returncode, 0)
        self.assertTrue((self.root / "renders/base.mp4").exists())
        result = self.cli("--apply", "--keep-renders", "final-v2.mp4", "shorts/v2/*.mp4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "renders/final-v2.mp4").exists())
        self.assertFalse((self.root / "renders/base.mp4").exists())

    def test_bad_manifests_fail_closed(self):
        for value in ["../outside.mp4", "renders/../../outside", "C:/outside",
                      "/outside", "renders/../final.mp4", "renders/x:stream",
                      "renders/missing*.mp4", "renders/final-v2.mp4."]:
            self.manifest([value])
            self.assertNotEqual(self.cli("--apply").returncode, 0, value)
            self.assertTrue((self.root / "work/clip.wav").exists())
        for content in ["{}", '{"keep": []}', '{"keep": "renders/*.mp4"}', "broken"]:
            self.write("edit/deliverables.json", content)
            self.assertNotEqual(self.cli("--apply").returncode, 0)

    def test_glob_boundaries(self):
        self.assertTrue(cleanup.matches(("renders", "x.mp4"), ("renders", "**", "*.mp4")))
        self.assertTrue(cleanup.matches(("renders", "a", "b", "x.mp4"), ("renders", "**", "*.mp4")))
        self.assertFalse(cleanup.matches(("renders", "a", "x.mp4"), ("renders", "*.mp4")))

    def junction(self, link, target):
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_external_junction_refused(self):
        outside = self.scratch / "outside"
        outside.mkdir()
        (outside / "valuable").write_text("keep")
        self.junction(self.root / "work/escape", outside)
        result = self.cli("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((outside / "valuable").exists())
        self.assertTrue((self.root / "work/clip.wav").exists())

    def test_internal_junction_preserved_not_followed(self):
        self.junction(self.root / "work/raw-link", self.root / "raw")
        result = self.cli("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.lexists(self.root / "work/raw-link"))
        self.assertTrue((self.root / "raw/clip.mp4").exists())

    def test_changed_file_and_replaced_directory_refused(self):
        tree = self.plan()
        self.write("work/clip.wav", "new render data")
        with self.assertRaises(cleanup.UnsafePath):
            cleanup.apply(self.root, tree, [])
        self.assertTrue((self.root / "overlays/title.mov").exists())
        tree = self.plan()
        old = self.root / "work/nested"
        old.rename(self.root / "work/renamed")
        self.junction(old, self.root / "raw")
        with self.assertRaises(cleanup.UnsafePath):
            cleanup.apply(self.root, tree, [])

    def test_partial_failure_logged(self):
        tree = self.plan()
        original = Path.unlink
        counter = 0
        def unlink(path, *args, **kwargs):
            nonlocal counter
            if path.is_relative_to(self.root / "work"):
                counter += 1
                if counter == 2:
                    raise PermissionError("injected locked file")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink), self.assertRaises(PermissionError):
            cleanup.apply(self.root, tree, [])
        log = json.loads((self.root / "edit/cleanup-log.json").read_text(encoding="utf-8"))
        self.assertEqual(log["runs"][0]["status"], "failed")
        self.assertTrue(log["runs"][0]["deleted"])

    def test_cache_scope_cannot_include_project(self):
        with patch.object(cleanup, "command", return_value=str(self.root)), patch.object(cleanup.shutil, "which", return_value=None):
            with self.assertRaises(cleanup.UnsafePath):
                cleanup.cache_plan(self.root)
        local = self.scratch / "local"
        nested_project = local / "Temp/ad-chrome-project/video"
        nested_project.mkdir(parents=True)
        pip = self.scratch / "safe-pip-cache"
        pip.mkdir()
        with patch.dict(os.environ, LOCALAPPDATA=str(local)), patch.object(cleanup, "command", return_value=str(pip)), patch.object(cleanup.shutil, "which", return_value=None):
            with self.assertRaises(cleanup.UnsafePath):
                cleanup.cache_plan(nested_project)

    def test_caches_dry_run_age_and_mocked_commands(self):
        local = self.scratch / "local"
        temp = local / "Temp"
        old = temp / "hyperframes-extract-cache-old"
        fresh = temp / "ad-chrome-fresh"
        unrelated = temp / "unrelated"
        pip = self.scratch / "pip-cache"
        npm = self.scratch / "npm-cache"
        for directory in [old, fresh, unrelated, pip, npm]:
            directory.mkdir(parents=True)
            (directory / "data").write_bytes(b"cache")
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        calls = []
        def command(args):
            calls.append(args)
            if args[-1] == "dir":
                return str(pip)
            if args[-3:] == ["config", "get", "cache"]:
                return str(npm)
            if "purge" in args:
                (pip / "data").unlink()
            elif "--force" in args:
                (npm / "data").unlink()
            else:
                self.fail(args)
            return "purged"
        with patch.dict(os.environ, LOCALAPPDATA=str(local)), patch.object(cleanup, "command", command), patch.object(cleanup.shutil, "which", return_value="npm.cmd"):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cleanup.main([str(self.root), "--purge-caches"]), 0)
            self.assertEqual(len(calls), 2)
            self.assertTrue((old / "data").exists())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cleanup.main([str(self.root), "--purge-caches", "--apply"]), 0)
        self.assertFalse(old.exists())
        for directory in [fresh, unrelated]:
            self.assertTrue((directory / "data").exists())
        self.assertFalse((pip / "data").exists())
        self.assertFalse((npm / "data").exists())
        purges = [args for args in calls if "purge" in args or "--force" in args]
        self.assertEqual(len(purges), 2)
        self.assertEqual(purges[0][0], getattr(sys, "_base_executable", sys.executable))
        log = json.loads((self.root / "edit/cleanup-log.json").read_text(encoding="utf-8"))
        self.assertEqual(len(log["runs"][0]["cache_commands"]), 2)
        self.assertTrue(any(item["path"] == str(pip / "data") for item in log["runs"][0]["deleted"]))


if __name__ == "__main__":
    unittest.main()
