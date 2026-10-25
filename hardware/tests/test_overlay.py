"""Compile and resolve the Linux PS-PL overlay without Vivado or private keys."""
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HARDWARE = Path(__file__).resolve().parents[1]


def generate_overlay(directory):
    script = HARDWARE / "vivado/package_overlay.tcl"
    if shutil.which("tclsh"):
        # The paths are Tcl arguments supplied through argv, not evaluated text.
        subprocess.run(
            ["tclsh", "/dev/stdin", str(script), str(directory)],
            input="source [lindex $argv 0]\nsiliconsign_write_overlay [lindex $argv 1]\n",
            text=True, check=True, capture_output=True,
        )
    else:
        try:
            import tkinter
        except ImportError:
            raise unittest.SkipTest("Tcl interpreter unavailable")
        interpreter = tkinter.Tcl()
        interpreter.call("source", str(script))
        interpreter.call("siliconsign_write_overlay", str(directory))


@unittest.skipUnless(all(shutil.which(p) for p in ("dtc", "fdtoverlay", "fdtget")),
                     "device-tree compiler utilities required")
class OverlayTests(unittest.TestCase):
    def test_generated_overlay_resolves_ps_configuration_without_private_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            generate_overlay(directory)
            self.assertEqual({p.name for p in directory.iterdir()},
                             {"siliconsign.dts", "siliconsign.dtbo"})
            # The base supplies only the published board symbols. Applying the
            # compiled overlay must resolve reset/clock phandles correctly.
            base = directory / "base.dts"
            base.write_text('''/dts-v1/;
/ {
    #address-cells = <2>; #size-cells = <2>;
    fpga_full: fpga-full { };
    amba: amba { #address-cells = <2>; #size-cells = <2>; };
    zynqmp_clk: clock-controller { #clock-cells = <1>; };
    zynqmp_reset: reset-controller { #reset-cells = <1>; };
};
''')
            subprocess.run(["dtc", "-@", "-I", "dts", "-O", "dtb", "-o",
                            str(directory / "base.dtb"), str(base)], check=True,
                           capture_output=True)
            merged = directory / "merged.dtb"
            subprocess.run(["fdtoverlay", "-i", str(directory / "base.dtb"),
                            "-o", str(merged), str(directory / "siliconsign.dtbo")],
                           check=True, capture_output=True)

            def get(node, prop, kind="x"):
                return subprocess.check_output(
                    ["fdtget", "-t", kind, str(merged), node, prop], text=True).strip()

            afi = "/amba/siliconsign_afi"
            self.assertEqual(get(afi, "compatible", "s"), "xlnx,afi-fpga")
            # Only the combined FPD master-width field is changed: active GP0
            # and disabled GP1 select 32 bits. HP/HPC/LPD registers are untouched.
            self.assertEqual(get(afi, "config-afi"), "e 0")
            reset_phandle = get("/reset-controller", "phandle")
            self.assertEqual(get(afi, "resets"),
                             " ".join(f"{reset_phandle} {reset:x}" for reset in range(116, 120)))
            self.assertEqual(get("/fpga-full", "firmware-name", "s"),
                             "siliconsign.bit.bin")
            self.assertEqual(get("/amba/siliconsign_clocking0", "assigned-clock-rates", "u"),
                             "50000000")
            self.assertEqual(get("/amba/siliconsign@a0000000", "reg"),
                             "0 a0000000 0 1000")
            # Recovery artifacts are exactly the generator's current output.
            for name in ("siliconsign.dts", "siliconsign.dtbo"):
                self.assertEqual((directory / name).read_bytes(),
                                 (HARDWARE / "recovery/r4" / name).read_bytes())

    def test_r3_is_preserved_as_the_historical_single_reset_image(self):
        # r3 remains evidence of the failed attempt, not the current generator.
        image = HARDWARE / "recovery/r3/siliconsign.dtbo"
        self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(),
                         "4b584849ba1a8d7bd808f1427632db4c73b8f62ac0a40affd6e3a24b43cad7ba")
        reset_cells = subprocess.check_output(
            ["fdtget", "-t", "x", str(image),
             "/fragment@1/__overlay__/siliconsign_afi", "resets"], text=True).split()
        self.assertEqual(reset_cells, ["ffffffff", "74"])


if __name__ == "__main__":
    unittest.main()
