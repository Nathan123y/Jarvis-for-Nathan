import cmath
import math
import unittest

from plugins import ee_tutor as ee


def num(text):
    """First number in a line like 'V(a) = 4 V' / 'V(a) = 2.5 mA'."""
    import re
    m = re.search(r"=\s*([-\d.e+]+)\s*([TGMkmµnpf]?)", text)
    scale = {"": 1, "T": 1e12, "G": 1e9, "M": 1e6, "k": 1e3, "m": 1e-3, "µ": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}
    return float(m.group(1)) * scale[m.group(2)]


def line(text, start):
    return next(l for l in text.splitlines() if l.strip().startswith(start))


class CalcTests(unittest.TestCase):
    def test_prefixes_units_and_parallel(self):
        self.assertAlmostEqual(ee.evaluate("10k + 4.7kΩ"), 14700)
        self.assertAlmostEqual(ee.evaluate("5mA * 2k"), 10)
        self.assertAlmostEqual(ee.evaluate("1meg"), 1e6)
        self.assertAlmostEqual(ee.evaluate("1M"), 1e6)
        self.assertAlmostEqual(ee.evaluate("4.7uF"), 4.7e-6)
        self.assertAlmostEqual(ee.evaluate("par(3k, 6k)"), 2000)
        self.assertAlmostEqual(ee.evaluate("1/(2*pi*1k*1u)"), 159.1549, places=3)

    def test_phasors_and_j(self):
        z = ee.evaluate("10∠30° + 4 - j3")
        self.assertAlmostEqual(z, cmath.rect(10, math.radians(30)) + 4 - 3j)
        self.assertAlmostEqual(ee.evaluate("ang(1 + j)"), 45)
        self.assertAlmostEqual(ee.evaluate("mag(3+j4)"), 5)
        self.assertAlmostEqual(ee.evaluate("cos(60°)"), 0.5)

    def test_steps_and_roots(self):
        out = ee.calc("tau = 2k*10u; v = 5*(1 - exp(-20m/tau))")
        self.assertIn("tau = 20 m", out)
        self.assertAlmostEqual(eval(out.split("full precision: ")[1].rstrip(")")), 5 * (1 - math.exp(-1)))
        r = ee.evaluate("roots(1, 3, 2)")
        for got, want in zip(sorted(r), [-2, -1]):
            self.assertAlmostEqual(got, want)

    def test_bad_input_is_explained(self):
        self.assertIn("2*pi", ee.run({"action": "calc", "expression": "2pi"}))
        self.assertIn("unknown name", ee.run({"action": "calc", "expression": "x + 1"}))


class CircuitTests(unittest.TestCase):
    def test_voltage_divider_dc_with_power(self):
        out = ee.solve_circuit("V1 in 0 12\nR1 in out 2k\nR2 out 0 4k")
        self.assertAlmostEqual(num(line(out, "V(out)")), 8)
        v1 = line(out, "V1:")
        self.assertAlmostEqual(num(v1.split("I =")[0].split("V =")[1].join(["=", ""])), 12)
        self.assertIn("I = -2 mA", v1)          # current enters + terminal: negative, delivering
        self.assertIn("delivering", v1)
        self.assertIn("balance", out)

    def test_current_source_direction(self):
        out = ee.solve_circuit("I1 0 a 2m\nR1 a 0 1k")      # arrow points at a
        self.assertAlmostEqual(num(line(out, "V(a)")), 2)

    def test_thevenin_with_dependent_source(self):
        # Alexander & Sadiku style: Vx across R2 controls a CCVS-free VCCS.
        net = "V1 1 0 10\nR1 1 a 4\nR2 a 0 6\nG1 0 a a 0 0.25"
        out = ee.solve_circuit(net, port="a")
        # KCL at a: (Va-10)/4 + Va/6 = 0.25 Va  ->  Va(1/4 + 1/6 - 1/4) = 2.5 -> Va = 15
        self.assertAlmostEqual(num(line(out, "Vth")), 15, places=6)
        # Test source: 1/Rth = 1/4 + 1/6 - 0.25 = 1/6 -> Rth = 6
        self.assertAlmostEqual(num(line(out, "Rth")), 6, places=6)

    def test_inverting_opamp(self):
        out = ee.solve_circuit("V1 in 0 1\nR1 in n 1k\nR2 n out 10k\nO1 out 0 n")
        self.assertIn("V(out) = -10 V", out)

    def test_ac_rc_lowpass_at_cutoff(self):
        out = ee.solve_circuit("V1 in 0 1∠0\nR1 in out 1k\nC1 out 0 1u", freq_hz="1/(2*pi*1m)")
        vout = line(out, "V(out)")
        self.assertIn("707.1 mV", vout)
        self.assertIn("∠ -45°", vout)

    def test_cccs_and_inductor_dc(self):
        out = ee.solve_circuit("V1 a 0 10\nR1 a b 5\nL1 b 0 1m\nF1 0 c R1 2\nR2 c 0 1")
        self.assertAlmostEqual(num(line(out, "L1:").split("I =")[1].join(["=", ""])), 2)
        self.assertAlmostEqual(num(line(out, "V(c)")), 4)

    def test_floating_node_is_reported(self):
        out = ee.solve_circuit("V1 a 0 5\nC1 a b 1u\nR1 b c 1k")
        self.assertIn("no unique solution", out)
        self.assertIn("capacitors are open", out)


class EquationAndCheckTests(unittest.TestCase):
    def test_nodal_equations(self):
        out = ee.solve_equations("(v1-12)/2k + v1/4k + (v1-v2)/1k = 0; (v2-v1)/1k + v2/2k = 0")
        self.assertIn("v1 =", out)
        a = [[1/2000 + 1/4000 + 1/1000, -1/1000], [-1/1000, 1/1000 + 1/2000]]
        import numpy as np
        v = np.linalg.solve(a, [12/2000, 0])
        self.assertAlmostEqual(num(line(out, "v1")), v[0], places=3)

    def test_mismatch_and_nonlinear(self):
        self.assertIn("unknown", ee.solve_equations("x + y = 1"))
        self.assertIn("aren't linear", ee.solve_equations("x*x = 4"))
        self.assertIn("no unique solution", ee.solve_equations("x + y = 1; 2x + 2y = 2".replace("2x", "2*x").replace("2y", "2*y")))

    def test_check_flags_common_slips(self):
        self.assertTrue(ee.check("2.4 mA", "2.4e-3").startswith("CORRECT"))
        self.assertIn("unit-prefix", ee.check("2.4", "2.4m"))
        self.assertIn("sign error", ee.check("-5", "5"))
        self.assertIn("peak used where RMS", ee.check("10", "10/sqrt(2)"))
        self.assertIn("Hz used where rad/s", ee.check("1k", "2*pi*1k"))
        self.assertIn("off by +90", ee.check("5∠30", "5∠-60"))
        self.assertIn("only the magnitude", ee.check("5", "5∠53.13"))
        self.assertTrue(ee.check("5∠53", "3+j4").startswith("CORRECT"))

    def test_reference_topics(self):
        self.assertIn("V+ = V−", ee.reference("op-amps"))
        self.assertIn("Topics:", ee.reference("quantum"))


if __name__ == "__main__":
    unittest.main()
