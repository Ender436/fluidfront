"""Regression locks for case generation: tunnel boundary faces and the
options the pinned engine actually supports."""

import json
import tempfile
import unittest
from pathlib import Path

from fluidfront.gen.schema import CaseError, load_case
from fluidfront.gen.setup_cpp import generate_setup

BASE = {
    "name": "t", "domain": {"nx": 64, "ny": 128, "nz": 1},
    "geometry": {"kind": "cylinder", "size_lu": 32, "center": [0.5, 0.25, 0.5]},
    "fluid": {"kind": "air"},
    "flow": {"mode": "tunnel", "u_si": 1.0, "length_si": 0.05, "u_lu": 0.1},
    "options": {"velocity_set": "d2q9", "precision": "fp32"},
    "run": {"t_steps": 100, "force_every": 10, "snapshot_every": 50},
}


def _spec(**updates):
    case = json.loads(json.dumps(BASE))
    case.update(updates)
    d = Path(tempfile.mkdtemp())
    (d / "case.json").write_text(json.dumps(case))
    return load_case(d)


TUNNEL_BC = "if(x==0u||x==NX-1u||y==0u||y==NY-1u{}" \
            ") {{ lbm.flags[n] = TYPE_E; }}{}"


class TestTunnelBoundary(unittest.TestCase):
    def test_3d_has_all_six_faces(self):
        spec = _spec(domain={"nx": 64, "ny": 128, "nz": 64},
                     options={"velocity_set": "d3q19", "precision": "fp32"})
        setup = generate_setup(spec)
        # the bottom (z==0) face was missing: an open floor silently
        # asymmetric-boundaries the tunnel
        self.assertIn(TUNNEL_BC.format("||z==0u||z==NZ-1u", ""), setup)

    def test_3d_ground_overrides_floor_with_solid(self):
        spec = _spec(domain={"nx": 64, "ny": 128, "nz": 64},
                     options={"velocity_set": "d3q19", "precision": "fp32"},
                     geometry=dict(BASE["geometry"], ground=True))
        setup = generate_setup(spec)
        self.assertIn(TUNNEL_BC.format("||z==0u||z==NZ-1u", ""), setup)
        self.assertIn("if(z==0u) { lbm.flags[n] = TYPE_S; }", setup)

    def test_2d_gets_no_z_faces(self):
        setup = generate_setup(_spec())
        self.assertIn(TUNNEL_BC.format("", ""), setup)
        self.assertNotIn("z==0u", setup)


class TestSchemaGuards(unittest.TestCase):
    def test_smagorinsky_const_rejected(self):
        # the pinned engine bakes C into the kernel; accepting the option
        # and ignoring it was worse than refusing it
        with self.assertRaises(CaseError):
            _spec(options={"velocity_set": "d2q9", "precision": "fp32",
                           "smagorinsky_const": 0.2})

    def test_unknown_subgrid_model_rejected(self):
        with self.assertRaises(CaseError):
            _spec(options={"velocity_set": "d2q9", "precision": "fp32",
                           "subgrid_model": "vreman"})

    def test_zero_force_every_rejected(self):
        with self.assertRaises(CaseError):
            _spec(run={"t_steps": 100, "force_every": 0})


if __name__ == "__main__":
    unittest.main()
