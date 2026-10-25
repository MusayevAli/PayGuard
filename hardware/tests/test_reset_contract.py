"""Exercise the pre-synthesis reset contract, including the physical r2 fault.

Vivado object access is substituted with a small topology fixture; the actual
Tcl checker runs unchanged. This is not a simulation of AMD's reset IP.
"""
import shutil
import subprocess
import unittest
from pathlib import Path

try:
    import tkinter
except ImportError:
    tkinter = None


HARDWARE = Path(__file__).resolve().parents[1]
CONTRACT = HARDWARE / "vivado/reset_contract.tcl"
FIXTURE = r"""
array set properties {
    reset,CONFIG.C_EXT_RESET_HIGH 0
    reset,CONFIG.C_AUX_RESET_HIGH 0
    one,CONFIG.CONST_WIDTH 1
    one,CONFIG.CONST_VAL 1
    zero,CONFIG.CONST_WIDTH 1
    zero,CONFIG.CONST_VAL 0
}
array set nets {
    reset/aux_reset_in high
    reset/dcm_locked high
    reset/mb_debug_sys_rst low
    one/dout high
    zero/dout low
}
proc get_property {property object} { return $::properties($object,$property) }
proc get_bd_pins {args} {
    set name [lindex $args end]
    if {[info exists ::nets($name)]} { return $name }
    return {}
}
proc get_bd_nets {args} { return $::nets([lindex $args end]) }
"""


@unittest.skipUnless(shutil.which("tclsh") or tkinter is not None, "Tcl interpreter required")
class ResetContractTests(unittest.TestCase):
    def evaluate(self, mutation=""):
        if not shutil.which("tclsh"):
            interpreter = tkinter.Tcl()
            interpreter.eval(FIXTURE)
            interpreter.call("source", str(CONTRACT))
            interpreter.eval(mutation)
            try:
                interpreter.call("siliconsign_assert_reset_wiring", "reset", "one", "zero")
                return subprocess.CompletedProcess([], 0, "", "")
            except tkinter.TclError as exc:
                return subprocess.CompletedProcess([], 2, "", str(exc))
        script = (FIXTURE + "\nsource [lindex $argv 0]\n" + mutation + "\n" +
                  "if {[catch {siliconsign_assert_reset_wiring reset one zero} error]} {\n"
                  "  puts stderr $error; exit 2\n}\n")
        return subprocess.run(["tclsh", "/dev/stdin", str(CONTRACT)],
                              input=script, text=True, capture_output=True)

    def test_inactive_auxiliary_and_debug_inputs_pass(self):
        result = self.evaluate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_r2_active_low_auxiliary_tied_low_is_rejected(self):
        result = self.evaluate("set nets(reset/aux_reset_in) low")
        self.assertEqual(result.returncode, 2)
        self.assertIn("reset/aux_reset_in", result.stderr)

    def test_other_asserted_or_unresolved_reset_inputs_are_rejected(self):
        mutations = {
            "aux_polarity": "set properties(reset,CONFIG.C_AUX_RESET_HIGH) 1",
            "external_polarity": "set properties(reset,CONFIG.C_EXT_RESET_HIGH) 1",
            "debug_asserted": "set nets(reset/mb_debug_sys_rst) high",
            "unlocked": "set nets(reset/dcm_locked) low",
            "wrong_constant": "set properties(one,CONFIG.CONST_VAL) 0",
            "wrong_width": "set properties(one,CONFIG.CONST_WIDTH) 2",
            "missing_pin": "unset nets(reset/aux_reset_in)",
            "unconnected_pin": "set nets(reset/aux_reset_in) {}",
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                result = self.evaluate(mutation)
                self.assertEqual(result.returncode, 2, result.stdout)

    def test_build_runs_resolved_contract_before_synthesis(self):
        build = (HARDWARE / "vivado/build_kv260.tcl").read_text()
        validation = build.index("\nvalidate_bd_design\n")
        contract = build.index("\nsiliconsign_assert_reset_wiring $reset $one $zero\n")
        synthesis = build.index("\nlaunch_runs synth_1")
        self.assertLess(validation, contract)
        self.assertLess(contract, synthesis)
        self.assertIn("source [file join $script_dir reset_contract.tcl]", build)


if __name__ == "__main__":
    unittest.main()
