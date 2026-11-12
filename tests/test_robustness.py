"""Regression locks for run-lifecycle robustness: the Windows job-object
orphan guard, stop-while-silent, rerun output clearing, and the validation
prepare-case registry."""

import ctypes
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fluidfront.gen.runner import _BasicLim, _ExtLim, run_case
from fluidfront.server.jobs import _clear_outputs


class TestWinJobStructs(unittest.TestCase):
    """Layout per the Win32 SDK; checkable on any 64-bit platform because
    ctypes packing matches MSVC x64. A wrong layout made SetInformationJobObject
    fail, silently disabling the orphan-process guard."""

    def test_basic_lim(self):
        # JOBOBJECT_BASIC_LIMIT_INFORMATION: two LARGE_INTEGER time limits
        # first, so LimitFlags sits at offset 16; sizeof is 64 on x64
        self.assertEqual(ctypes.sizeof(_BasicLim), 64)
        self.assertEqual(_BasicLim.PerProcessUserTimeLimit.offset, 0)
        self.assertEqual(_BasicLim.PerJobUserTimeLimit.offset, 8)
        self.assertEqual(_BasicLim.LimitFlags.offset, 16)

    def test_ext_lim(self):
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION: sizeof 144 on x64
        self.assertEqual(ctypes.sizeof(_ExtLim), 144)
        self.assertEqual(_ExtLim.BasicLimitInformation.offset, 0)
        self.assertEqual(_ExtLim.IoInfo.offset, 64)


class TestStopWatchdog(unittest.TestCase):
    def test_stop_silent_child(self):
        """Stop must work even when the engine prints nothing (kernel
        compile, hang): the cancellation watchdog is independent of stdout."""
        d = tempfile.mkdtemp()
        try:
            script = os.path.join(d, "silent.sh")
            with open(script, "w") as f:
                f.write("#!/bin/sh\nexec sleep 60\n")  # silent direct child,
            os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)  # like the engine
            cancel = threading.Event()
            threading.Timer(0.3, cancel.set).start()
            t0 = time.time()
            run_case(script, d, cancel=cancel)
            self.assertLess(time.time() - t0, 8.0,
                            "silent child not stopped promptly")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_normal_run_not_killed(self):
        d = tempfile.mkdtemp()
        try:
            script = os.path.join(d, "quick.sh")
            with open(script, "w") as f:
                f.write("#!/bin/sh\necho hello\n")
            os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
            rc = run_case(script, d, cancel=threading.Event())
            self.assertEqual(rc, 0)
            with open(os.path.join(d, "run.log")) as f:
                self.assertIn("hello", f.read())
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestClearOutputs(unittest.TestCase):
    def test_removes_previous_run_results(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "output")
            os.makedirs(out)
            stale = ["u-100.vtk", "rho-50.vtk", "flags-0.vtk",
                     "u-150.vtk.part", "forces.csv"]
            keep = "notes.txt"
            for f in stale + [keep]:
                Path(out, f).write_text("x")
            _clear_outputs(Path(d))
            self.assertEqual(sorted(os.listdir(out)), [keep])
            _clear_outputs(Path(d))  # idempotent, works with missing files too
            _clear_outputs(Path(d) / "nonexistent")  # creates output/
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestValidateRegistry(unittest.TestCase):
    """prepare_* callbacks are registered explicitly; name-pattern lookup
    (prepare_<case name>) previously mismatched and left sphere3d without
    its STL, failing on every fresh checkout."""

    def test_stl_cases_have_prep(self):
        from fluidfront.validate import runner as vr
        prepared = {c["name"] for c, _ in vr.CASES
                    if c["geometry"].get("kind") == "stl"}
        for name in prepared:
            self.assertIn(name, vr._PREPARES,
                          f"case {name} needs an STL prep callback")

    def test_registry_consistency(self):
        from fluidfront.validate import runner as vr
        names = {c["name"] for c, _ in vr.CASES}
        self.assertLessEqual(set(vr._PREPARES), names)
        for cb in vr._PREPARES.values():
            self.assertTrue(callable(cb))

    def test_sphere_stl_is_created(self):
        from fluidfront.validate import runner as vr
        d = tempfile.mkdtemp()
        try:
            vr._PREPARES[vr.SPHERE3D["name"]](Path(d))
            self.assertTrue((Path(d) / "model.stl").is_file())
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
