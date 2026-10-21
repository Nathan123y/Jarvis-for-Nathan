"""
JARVIS plugin — circuits / EE tutor that computes instead of guessing.

The live voice model is quick and knows the theory, but it does arithmetic in
its head, and in a long homework session that is where it goes wrong: a dropped
kilo, a sign flipped on a dependent source, a phase angle off by 90 degrees —
and then, worse, it agrees that the student's wrong answer is right. This
plugin gives it something exact to lean on:

  calc             a calculator that understands EE notation: 10k, 4.7uF, 5mA,
                   j, 10∠30°, par(R1, R2), db(), roots(...), and "x = ..." steps
  solve_circuit    a full nodal (MNA) solver for DC and AC phasor circuits from
                   a small netlist: R, L, C, independent and all four dependent
                   sources, ideal op-amps; node voltages, every element's
                   current and power, and Thevenin/Norton at any port
  solve_equations  a linear system (the KCL/KVL/mesh equations the student
                   wrote) solved exactly, real or complex
  check            compares the student's answer with a computed one and names
                   the classic slip when it is off (sign, ×1000, peak vs RMS,
                   Hz vs rad/s, phase off by 90° or 180°, ...)
  reference        short, careful formula sheets with the usual pitfalls

Pure Python + numpy (already a core dependency). Nothing leaves the machine.
"""
from __future__ import annotations

import ast
import cmath
import math
import re

import numpy as np

PLUGIN = {
    "name": "ee_tutor",
    "description": (
        "Exact math for circuits / electrical engineering homework — use it for EVERY number "
        "you state or confirm while tutoring, never mental arithmetic. action=calc evaluates "
        "expressions with EE notation (10k, 4.7uF, 5mA, j, 10∠30°, par(a,b), db(x), "
        "roots(a,b,c), sqrt, exp, ln, log10, sind/cosd, mag(), ang(); 'tau = R*C; ...' steps). "
        "action=solve_circuit solves a netlist (DC, or AC at freq_hz/omega): node voltages, "
        "element currents and powers, Thevenin/Norton at a port. action=solve_equations "
        "solves the student's linear KCL/KVL/mesh equations. action=check compares the "
        "student's answer to the correct one you computed and names the likely mistake — "
        "ALWAYS run it before telling the user an answer is right or wrong. "
        "action=reference returns a formula sheet with pitfalls for a topic. "
        "NETLIST, one element per line, nodes are names, ground is 0: "
        "'R1 a b 10k' / 'C1 a 0 1u' / 'L1 a b 2m'; "
        "'V1 plus minus 12' (AC phasor: 10∠30); "
        "'I1 from to 2m' (current flows from→to through the source, arrow points at 'to'); "
        "'E1 plus minus cplus cminus gain' (VCVS); 'G1 from to cplus cminus gm' (VCCS); "
        "'F1 from to Rx gain' (CCCS, controlled by the current through element Rx); "
        "'H1 plus minus Rx r' (CCVS); 'O1 out inplus inminus' (ideal op-amp). "
        "Element currents are reported entering the FIRST node (passive sign convention), "
        "so negative power means the element delivers power."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "calc | solve_circuit | solve_equations | check | reference",
            },
            "expression": {
                "type": "STRING",
                "description": "For calc: the expression, or several steps separated by ';' like 'R = par(2k,3k); I = 12/R'",
            },
            "netlist": {
                "type": "STRING",
                "description": "For solve_circuit: one element per line (or separated by ';'), format in the tool description",
            },
            "freq_hz": {"type": "STRING", "description": "For solve_circuit: AC frequency in Hz. Leave empty for DC"},
            "omega": {"type": "STRING", "description": "For solve_circuit: AC angular frequency in rad/s (instead of freq_hz)"},
            "port": {
                "type": "STRING",
                "description": "For solve_circuit: two nodes 'a,b' to find the Thevenin/Norton equivalent seen there ('a' alone means a to ground)",
            },
            "phasors": {
                "type": "STRING",
                "description": "For AC power: 'peak' (default, amplitudes) or 'rms' — match the course's convention",
            },
            "equations": {
                "type": "STRING",
                "description": "For solve_equations: equations separated by ';' or newlines, e.g. '(v1-12)/2k + v1/4k + (v1-v2)/1k = 0; ...'",
            },
            "variables": {
                "type": "STRING",
                "description": "Known values for calc/solve_equations/check, e.g. 'R1=2k, C=10u, w=377'",
            },
            "answer": {"type": "STRING", "description": "For check: the student's answer, e.g. '3.2 mA' or '4.5∠-30'"},
            "correct": {
                "type": "STRING",
                "description": "For check: the correct value, which YOU computed with calc/solve_circuit/solve_equations (an expression is fine)",
            },
            "tolerance_percent": {"type": "STRING", "description": "For check: allowed rounding difference, default 2"},
            "topic": {
                "type": "STRING",
                "description": ("For reference: sign_conventions, nodal_mesh, thevenin_norton, opamps, first_order, "
                                "second_order, phasors_ac, ac_power, frequency_response, laplace, diodes"),
            },
        },
        "required": ["action"],
    },
}


# ── EE-aware calculator ──────────────────────────────────────────────────────

_PREFIX = {"T": 1e12, "G": 1e9, "meg": 1e6, "Meg": 1e6, "MEG": 1e6, "M": 1e6,
           "k": 1e3, "K": 1e3, "m": 1e-3, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
           "n": 1e-9, "p": 1e-12, "f": 1e-15}
_UNIT = r"(?:ohms|ohm|Ohms|Ohm|Ω|VAR|VA|Hz|V|A|W|F|H|S|s)"
# A number, an optional SI prefix and an optional unit, standing alone:
# "10k", "4.7uF", "5mA", "3.2 mA", "10kΩ". "R1" and "2pi" are left alone.
_NUM = re.compile(r"(?<![\w.])((?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*"
                  r"(meg|Meg|MEG|[TGMkKmuµμnpf])?" + _UNIT + r"?(?![\w.])")
_ANGLE = re.compile(r"∠\s*([-+]?\s*(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*°?")
_JPREFIX = re.compile(r"(?<![\w.])j(?=\s*[\d(.])")


def _prep(text: str) -> str:
    s = str(text).strip()
    s = s.replace("−", "-").replace("×", "*").replace("·", "*").replace("÷", "/").replace("^", "**")
    s = _ANGLE.sub(lambda m: "@(" + m.group(1).replace(" ", "") + ")", s)   # 10∠30° -> 10@(30)
    s = s.replace("°", "*_deg")
    s = _JPREFIX.sub("1j*", s)                                              # j3, -j10 -> 1j*3
    s = _NUM.sub(lambda m: f"({m.group(1)}*{_PREFIX[m.group(2)]!r})" if m.group(2) else m.group(1), s)
    return s


def _polar(mag, deg):
    return mag * cmath.exp(1j * math.radians(_real(deg, "an angle")))


def _real(v, what="that value"):
    v = complex(v)
    if abs(v.imag) > 1e-12 * max(1.0, abs(v.real)):
        raise ValueError(f"{what} must be a real number")
    return v.real


def _par(*zs):
    if any(z == 0 for z in zs):
        return 0
    return 1 / sum(1 / z for z in zs)


def _roots(*coeffs):
    r = np.roots([complex(c) for c in coeffs])
    return tuple(_clean(x) for x in r)


_FUNCS = {
    "sqrt": cmath.sqrt, "exp": cmath.exp, "ln": cmath.log, "log10": cmath.log10,
    "sin": cmath.sin, "cos": cmath.cos, "tan": cmath.tan,
    "asin": cmath.asin, "acos": cmath.acos, "atan": cmath.atan,
    "sind": lambda d: math.sin(math.radians(_real(d))),
    "cosd": lambda d: math.cos(math.radians(_real(d))),
    "tand": lambda d: math.tan(math.radians(_real(d))),
    "asind": lambda x: math.degrees(math.asin(_real(x))),
    "acosd": lambda x: math.degrees(math.acos(_real(x))),
    "atand": lambda x: math.degrees(math.atan(_real(x))),
    "atan2d": lambda y, x: math.degrees(math.atan2(_real(y), _real(x))),
    "abs": abs, "mag": abs, "ang": lambda z: math.degrees(cmath.phase(z)),
    "angle": lambda z: math.degrees(cmath.phase(z)),
    "re": lambda z: complex(z).real, "im": lambda z: complex(z).imag,
    "real": lambda z: complex(z).real, "imag": lambda z: complex(z).imag,
    "conj": lambda z: complex(z).conjugate(), "polar": _polar,
    "par": _par, "db": lambda x: 20 * math.log10(abs(x)), "dbp": lambda x: 10 * math.log10(abs(x)),
    "rad": lambda d: math.radians(_real(d)), "deg": lambda r: math.degrees(_real(r)),
    "min": lambda *a: min(_real(x) for x in a), "max": lambda *a: max(_real(x) for x in a),
    "roots": _roots,
}
_CONSTS = {"pi": math.pi, "e": math.e, "j": 1j, "_deg": math.pi / 180}


def _eval(node, names: dict):
    if isinstance(node, ast.Expression):
        return _eval(node.body, names)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, complex)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise NameError(f"unknown name '{node.id}' (give it a value, or write 2*pi rather than 2pi)")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand, names)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, names), _eval(node.right, names)
        op = node.op
        if isinstance(op, ast.Add): return a + b
        if isinstance(op, ast.Sub): return a - b
        if isinstance(op, ast.Mult): return a * b
        if isinstance(op, ast.Div): return a / b
        if isinstance(op, ast.Pow):
            if abs(complex(b)) > 1000:
                raise ValueError("exponent too large")
            return a ** b
        if isinstance(op, ast.MatMult): return _polar(a, b)      # magnitude ∠ degrees
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS \
            and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a, names) for a in node.args])
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, names) for e in node.elts)
    raise ValueError("unsupported expression")


def evaluate(text: str, names: dict | None = None):
    """Evaluate one EE expression. Raises ValueError/NameError/ZeroDivisionError on bad input."""
    if len(str(text)) > 2000:
        raise ValueError("expression too long")
    try:
        tree = ast.parse(_prep(text), mode="eval")
    except SyntaxError:
        raise ValueError(f"could not read '{text}' (use * for multiplication, e.g. 2*pi*f)")
    return _clean(_eval(tree, names or {}))


def _clean(v):
    if isinstance(v, tuple):
        return tuple(_clean(x) for x in v)
    v = complex(v)
    if abs(v.imag) <= 1e-12 * max(1.0, abs(v.real)):
        return float(v.real)
    if abs(v.real) <= 1e-12 * abs(v.imag):
        return complex(0.0, v.imag)
    return v


_ENG = {12: "T", 9: "G", 6: "M", 3: "k", 0: "", -3: "m", -6: "µ", -9: "n", -12: "p", -15: "f"}


def eng(x: float, unit: str = "") -> str:
    if x == 0 or not math.isfinite(x):
        return f"{x:g} {unit}".strip()
    e3 = max(-15, min(12, int(math.floor(math.log10(abs(x)) / 3) * 3)))
    m = x / 10 ** e3
    s = f"{m:.4g}"
    return f"{s} {_ENG[e3]}{unit}".strip()


def fmt(v, unit: str = "") -> str:
    if isinstance(v, tuple):
        return ", ".join(fmt(x, unit) for x in v)
    v = _clean(v)
    if isinstance(v, float):
        return eng(v, unit)
    mag, ang = abs(v), math.degrees(cmath.phase(v))
    sign = "+" if v.imag >= 0 else "-"
    return (f"{eng(mag, unit)} ∠ {ang:.4g}°  (= {v.real:.4g} {sign} j{abs(v.imag):.4g}"
            f"{' ' + unit if unit else ''})")


def _parse_vars(text: str) -> dict:
    names: dict = {}
    # Commas separate assignments, but not the commas inside polar(5, 30).
    for part in re.split(r"[;\n]|,(?=\s*[A-Za-z_]\w*\s*=)", str(text or "")):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k.isidentifier() and k not in _FUNCS:
                names[k] = evaluate(v, names)
    return names


def calc(expression: str, variables: str = "") -> str:
    names = _parse_vars(variables)
    lines = []
    steps = [s for s in re.split(r"[;\n]", expression) if s.strip()]
    if not steps:
        return "What should I calculate?"
    result = None
    for step in steps:
        m = re.match(r"^\s*([A-Za-z_]\w*)\s*=(?!=)(.*)$", step)
        if m and m.group(1) not in _FUNCS:
            name, expr = m.group(1), m.group(2)
            result = evaluate(expr, names)
            names[name] = result
            lines.append(f"{name} = {fmt(result)}")
        else:
            result = evaluate(step, names)
            lines.append(f"{step.strip()} = {fmt(result)}")
    exact = "" if isinstance(result, tuple) else f"\n(full precision: {result!r})"
    return "\n".join(lines) + exact


# ── Circuit solver (modified nodal analysis) ─────────────────────────────────

_GROUND = {"0", "gnd", "GND", "ground", "Gnd"}
_ARITY = {"R": 2, "C": 2, "L": 2, "V": 2, "I": 2, "E": 4, "G": 4, "F": 2, "H": 2, "O": 3}


class CircuitError(ValueError):
    pass


def _parse_netlist(text: str, names: dict) -> list[dict]:
    elems, seen = [], set()
    for raw in re.split(r"[\n;]", str(text or "")):
        line = raw.strip()
        if not line or line[0] in "*#":
            continue
        tok = line.split()
        name = tok[0]
        kind = name[0].upper()
        if kind not in _ARITY:
            raise CircuitError(f"'{line}': element names start with R, C, L, V, I, E, G, F, H or O")
        if name.upper() in seen:
            raise CircuitError(f"two elements are named {name}")
        seen.add(name.upper())
        n = _ARITY[kind]
        if kind in "FH":
            if len(tok) < 5:
                raise CircuitError(f"'{line}': expected {name} node1 node2 controlling_element gain")
            nodes, ctrl, rest = tok[1:3], tok[3], tok[4:]
        else:
            if len(tok) < n + 1 + (0 if kind == "O" else 1):
                raise CircuitError(f"'{line}': expected {n} nodes and a value")
            nodes, ctrl, rest = tok[1:n + 1], None, tok[n + 1:]
        value = None
        if kind != "O":
            if rest and rest[0].upper() in ("DC", "AC"):
                rest = rest[1:]
            try:
                value = evaluate(" ".join(rest), names)
            except Exception as exc:
                raise CircuitError(f"'{line}': could not read the value ({exc})")
        nodes = ["0" if x in _GROUND else x for x in nodes]
        elems.append({"name": name, "kind": kind, "nodes": nodes, "ctrl": ctrl, "value": value})
    if not elems:
        raise CircuitError("the netlist is empty")
    return elems


class _Circuit:
    def __init__(self, elems: list[dict], omega: float | None):
        self.elems, self.omega = elems, omega
        self.by_name = {e["name"].upper(): e for e in elems}
        nodes = []
        for e in elems:
            for x in e["nodes"]:
                if x != "0" and x not in nodes:
                    nodes.append(x)
        if not nodes:
            raise CircuitError("every element is connected only to ground")
        self.nodes = nodes
        self.idx = {x: i for i, x in enumerate(nodes)}
        k = len(nodes)
        for e in elems:
            e["branch"] = None
            if e["kind"] in "VEHO" or (e["kind"] == "L" and omega is None):
                e["branch"] = k
                k += 1
            e["Y"] = self._admittance(e)
        self.size = k
        for e in elems:
            if e["kind"] in "FH":
                c = self.by_name.get(str(e["ctrl"]).upper())
                if c is None:
                    raise CircuitError(f"{e['name']} is controlled by '{e['ctrl']}', which is not in the netlist")
                if c["kind"] in "IFG":
                    raise CircuitError(f"{e['name']}: control it by a resistor or a (0 V) voltage source, not {c['name']}")

    def _admittance(self, e):
        v, kind, w = e["value"], e["kind"], self.omega
        if kind == "R":
            if v == 0:
                raise CircuitError(f"{e['name']} is 0 Ω; use a 0 V source for a wire")
            return 1 / v
        if kind == "C":
            return None if w is None else 1j * w * v
        if kind == "L" and w is not None:
            if w == 0 or v == 0:
                raise CircuitError(f"{e['name']}: an inductor at 0 rad/s is a short; solve as DC instead")
            return 1 / (1j * w * v)
        return None

    def _n(self, node):
        return None if node == "0" else self.idx[node]

    def current_terms(self, e) -> tuple[dict, complex]:
        """Current through `e`, entering its first node: ({unknown: coeff}, constant)."""
        a, b = (self._n(x) for x in e["nodes"][:2])
        if e["branch"] is not None:
            return {e["branch"]: 1.0}, 0
        if e["Y"] is not None:
            t = {}
            if a is not None: t[a] = t.get(a, 0) + e["Y"]
            if b is not None: t[b] = t.get(b, 0) - e["Y"]
            return t, 0
        if e["kind"] == "I":
            return {}, e["value"]
        if e["kind"] == "G":
            c, d = (self._n(x) for x in e["nodes"][2:4])
            t = {}
            if c is not None: t[c] = t.get(c, 0) + e["value"]
            if d is not None: t[d] = t.get(d, 0) - e["value"]
            return t, 0
        if e["kind"] == "F":
            t, k = self.current_terms(self.by_name[e["ctrl"].upper()])
            return {i: e["value"] * v for i, v in t.items()}, e["value"] * k
        return {}, 0                                    # capacitor at DC: open

    def matrices(self, sources_on: bool = True):
        n = self.size
        A = np.zeros((n, n), dtype=complex)
        z = np.zeros(n, dtype=complex)

        def add(r, c, v):
            if r is not None and c is not None:
                A[r, c] += v

        for e in self.elems:
            kind = e["kind"]
            a, b = (self._n(x) for x in e["nodes"][:2])
            r = e["branch"]
            if e["Y"] is not None:
                Y = e["Y"]
                add(a, a, Y); add(b, b, Y); add(a, b, -Y); add(b, a, -Y)
            elif kind in "IGF":
                t, const = self.current_terms(e)
                if not sources_on and kind == "I":
                    const = 0
                for col, v in t.items():
                    add(a, col, v); add(b, col, -v)
                if a is not None: z[a] -= const
                if b is not None: z[b] += const
            if r is None:
                continue
            if kind == "O":
                p, m = self._n(e["nodes"][1]), self._n(e["nodes"][2])
                add(a, r, 1)
                add(r, p, 1); add(r, m, -1)
                continue
            add(a, r, 1); add(b, r, -1)
            add(r, a, 1); add(r, b, -1)
            if kind == "V":
                z[r] = e["value"] if sources_on else 0
            elif kind == "E":
                c, d = (self._n(x) for x in e["nodes"][2:4])
                add(r, c, -e["value"]); add(r, d, e["value"])
            elif kind == "H":
                t, const = self.current_terms(self.by_name[e["ctrl"].upper()])
                for col, v in t.items():
                    add(r, col, -e["value"] * v)
                z[r] += e["value"] * (const if sources_on else 0)
        return A, z

    def solve(self, sources_on: bool = True, inject: tuple | None = None):
        A, z = self.matrices(sources_on)
        if inject:
            a, b = (self._n(x) for x in inject)
            if a is not None: z[a] += 1
            if b is not None: z[b] -= 1
        if np.linalg.cond(A) > 1e13:
            raise np.linalg.LinAlgError("singular")
        return np.linalg.solve(A, z)

    def voltage(self, x, node):
        return 0 if node == "0" else x[self.idx[node]]

    def current(self, x, e):
        t, const = self.current_terms(e)
        return sum(v * x[i] for i, v in t.items()) + const


def solve_circuit(netlist: str, freq_hz: str = "", omega: str = "", port: str = "",
                  phasors: str = "peak", variables: str = "") -> str:
    names = _parse_vars(variables)
    w = None
    try:
        if str(omega).strip():
            w = _real(evaluate(omega, names), "omega")
        elif str(freq_hz).strip():
            w = 2 * math.pi * _real(evaluate(freq_hz, names), "freq_hz")
        if w is not None and w <= 0:
            w = None                                    # 0 Hz is DC
        elems = _parse_netlist(netlist, names)
        ckt = _Circuit(elems, w)
        x = ckt.solve()
    except CircuitError as exc:
        return f"Netlist problem: {exc}"
    except np.linalg.LinAlgError:
        return ("The circuit has no unique solution: look for a node with no path to ground"
                + (" (at DC capacitors are open, so a node only reached through a capacitor floats)" if w is None else "")
                + ", a loop made only of voltage sources/inductors, or a current source in series with nothing else.")
    except (ValueError, ZeroDivisionError, NameError) as exc:
        return f"Could not read the analysis settings: {exc}"

    rms = str(phasors).strip().lower() == "rms"
    if w is None:
        head = "DC analysis (capacitors open, inductors short)."
    else:
        head = (f"AC analysis at f = {eng(w / (2 * math.pi), 'Hz')}, ω = {w:.6g} rad/s; phasors are "
                + ("RMS" if rms else "peak amplitudes") + ".")
    out = [head, "", "Node voltages (to ground):"]
    for node in ckt.nodes:
        out.append(f"  V({node}) = {fmt(_clean(ckt.voltage(x, node)), 'V')}")
    out += ["", "Elements (voltage = first node − second node, current entering the first node):"]
    total, gross = 0, 0.0
    for e in elems:
        a, b = e["nodes"][:2]
        if e["kind"] == "O":
            i_out = -x[e["branch"]]
            out.append(f"  {e['name']} (op-amp): V(out) = {fmt(_clean(ckt.voltage(x, a)), 'V')}, "
                       f"output current into node {a} = {fmt(_clean(i_out), 'A')}")
            continue
        v = ckt.voltage(x, a) - ckt.voltage(x, b)
        i = ckt.current(x, e)
        if w is None:
            p = v * i
        else:
            p = v * np.conj(i) * (1 if rms else 0.5)
        total += p
        gross += abs(complex(p))
        if w is None:
            ptxt = f"P = {fmt(_clean(p), 'W')} " + ("absorbed" if _clean(p).real >= 0 else "(delivering)")
        else:
            s = complex(p)
            s = complex(0 if abs(s.real) < 1e-9 * abs(s) else s.real,
                        0 if abs(s.imag) < 1e-9 * abs(s) else s.imag)
            ptxt = (f"P = {eng(s.real, 'W')}, Q = {eng(s.imag, 'VAR')}"
                    + ("" if s.real >= -1e-15 else " (delivering)"))
        extra = ""
        if e["kind"] in "RCL" and w is not None:
            zval = {"R": e["value"], "C": 1 / (1j * w * e["value"]), "L": 1j * w * e["value"]}[e["kind"]]
            extra = f", Z = {fmt(_clean(zval), 'Ω')}"
        out.append(f"  {e['name']}: V = {fmt(_clean(v), 'V')}, I = {fmt(_clean(i), 'A')}, {ptxt}{extra}")
    if not any(e["kind"] == "O" for e in elems):
        bal = complex(total)
        out.append("Power check: sources and loads balance."
                   if abs(bal) <= 1e-7 * gross + 1e-15 else
                   f"Power check: net {fmt(_clean(bal))} (should be ~0)")

    if str(port).strip():
        pts = [p.strip() for p in re.split(r"[,\s]+", str(port).strip()) if p.strip()]
        pa, pb = (pts + ["0"])[:2]
        pa, pb = ("0" if pa in _GROUND else pa), ("0" if pb in _GROUND else pb)
        if any(p != "0" and p not in ckt.idx for p in (pa, pb)):
            out.append(f"\nThevenin: port node not found in the netlist ({port}).")
        else:
            voc = ckt.voltage(x, pa) - ckt.voltage(x, pb)
            out.append(f"\nThevenin / Norton seen at {pa}–{pb} (Vth = V({pa}) − V({pb})):")
            out.append(f"  Vth (open circuit) = {fmt(_clean(voc), 'V')}")
            try:
                xt = ckt.solve(sources_on=False, inject=(pa, pb))
                zth = _clean(ckt.voltage(xt, pa) - ckt.voltage(xt, pb))
                out.append(f"  Rth/Zth = {fmt(zth, 'Ω')} (independent sources off, dependent sources kept)")
                if abs(zth) > 1e-12:
                    out.append(f"  In (short circuit, {pa}→{pb}) = {fmt(_clean(voc / zth), 'A')}")
                    zr = complex(zth).real
                    if zr > 0:
                        k = 4 if (w is None or rms) else 8
                        pmax = abs(voc) ** 2 / (k * zr)
                        load = eng(zr, "Ω") if w is None else fmt(_clean(complex(zth).conjugate()), "Ω")
                        out.append(f"  Max power transfer: load = {load}, Pmax = {eng(pmax, 'W')}")
                else:
                    out.append("  Rth ≈ 0: the port looks like an ideal voltage source (no Norton equivalent).")
            except np.linalg.LinAlgError:
                out.append("  Rth is infinite with sources off: the port looks like an ideal current source "
                           "(no Thevenin equivalent; use Norton).")
    return "\n".join(out)


# ── Linear equation solver ───────────────────────────────────────────────────

def _names_in(tree) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in _FUNCS and node.id not in _CONSTS \
                and node.id not in found:
            found.append(node.id)
    return found


def solve_equations(equations: str, variables: str = "") -> str:
    known = _parse_vars(variables)
    sides, unknowns = [], []
    for raw in re.split(r"[;\n]", str(equations or "")):
        if not raw.strip():
            continue
        parts = re.split(r"(?<![=<>!])=(?!=)", raw)
        if len(parts) > 2:
            return f"'{raw.strip()}' has more than one '='."
        lhs, rhs = (parts + ["0"])[:2]
        try:
            tl, tr = (ast.parse(_prep(s), mode="eval") for s in (lhs, rhs))
        except SyntaxError:
            return f"Could not read '{raw.strip()}' (use * for multiplication)."
        sides.append((tl, tr))
        for n in _names_in(tl) + _names_in(tr):
            if n not in known and n not in unknowns:
                unknowns.append(n)
    if not sides:
        return "Give me the equations, separated by semicolons."
    if len(sides) != len(unknowns):
        return (f"{len(sides)} equation(s) but {len(unknowns)} unknown(s) "
                f"({', '.join(unknowns) or 'none'}). They need to match; give known values in variables.")

    def residual(vec):
        names = {**known, **dict(zip(unknowns, vec))}
        return np.array([complex(_eval(l, names)) - complex(_eval(r, names)) for l, r in sides])

    n = len(unknowns)
    try:
        b0 = residual(np.zeros(n))
        A = np.column_stack([residual(np.eye(n)[i]) - b0 for i in range(n)])
        probe = np.linspace(0.37, 1.71, n) * (1 + 0.29j)
        pred = A @ probe + b0
        if not np.allclose(residual(probe), pred, rtol=1e-7, atol=1e-9 * (np.abs(b0).max() + np.abs(A).max())):
            raise ZeroDivisionError
    except ZeroDivisionError:
        return ("Those equations aren't linear in the unknowns (an unknown is multiplied by another, "
                "divided into, or inside a function). I only solve linear systems; check whether a "
                "value meant to be known was left out of variables.")
    except (ValueError, NameError, TypeError) as exc:
        return f"Could not evaluate the equations: {exc}"
    if np.linalg.cond(A) > 1e13:
        return ("Those equations have no unique solution: at least one equation is a combination of "
                "the others (a classic sign of writing KCL at every node including the reference, or "
                "missing a supernode/supermesh constraint).")
    sol = np.linalg.solve(A, -b0)
    return "Solution:\n" + "\n".join(f"  {u} = {fmt(_clean(v))}" for u, v in zip(unknowns, sol))


# ── Answer checking ──────────────────────────────────────────────────────────

_SLIPS = [
    (-1, "a sign error (reversed reference direction or polarity)"),
    (1e3, "off by 1000: a unit-prefix slip (mA vs A, kΩ vs Ω, ms vs s)"),
    (1e-3, "off by 1000: a unit-prefix slip (mA vs A, kΩ vs Ω, ms vs s)"),
    (1e6, "off by a million: a prefix slip (µ vs base unit, MΩ vs Ω)"),
    (1e-6, "off by a million: a prefix slip (µ vs base unit, MΩ vs Ω)"),
    (2, "twice the right value (a doubled term, or ½ missing, e.g. ½CV² or ½VI* for peak phasors)"),
    (0.5, "half the right value (an extra ½, or a factor of 2 dropped)"),
    (math.sqrt(2), "√2 too large: peak used where RMS was needed"),
    (1 / math.sqrt(2), "√2 too small: RMS used where peak was needed"),
    (2 * math.pi, "2π too large: rad/s used where Hz was needed"),
    (1 / (2 * math.pi), "2π too small: Hz used where rad/s (ω = 2πf) was needed"),
]


def check(answer: str, correct: str, tolerance_percent: str = "", variables: str = "") -> str:
    names = _parse_vars(variables)
    try:
        s = complex(evaluate(answer, names))
        c = complex(evaluate(correct, names))
        tol = float(evaluate(tolerance_percent, {})) / 100 if str(tolerance_percent).strip() else 0.02
    except Exception as exc:
        return f"Could not read the values to compare: {exc}"
    if c == 0:
        ok = abs(s) < 1e-9
        return ("CORRECT: both are zero." if ok else
                f"INCORRECT: the answer should be 0, the student has {fmt(_clean(s))}.")
    err = abs(s - c) / abs(c)
    if err <= tol:
        return f"CORRECT: {fmt(_clean(s))} matches {fmt(_clean(c))} (within {err * 100:.2g}%)."
    out = [f"INCORRECT: the student has {fmt(_clean(s))}, the correct value is {fmt(_clean(c))} "
           f"({err * 100:.3g}% off)."]
    ratio = s / c
    for factor, label in _SLIPS:
        if abs(ratio - factor) <= max(tol, 0.01) * abs(factor):
            out.append(f"Likely cause: {label}.")
            break
    else:
        if abs(s.imag) < 1e-12 and abs(c.imag) > 1e-12 * abs(c) and abs(abs(s) - abs(c)) <= tol * abs(c):
            out.append("The student gave only the magnitude; the full answer is a phasor with an angle.")
        elif abs(abs(s) - abs(c)) <= tol * abs(c) and (s.imag or c.imag):
            d = (math.degrees(cmath.phase(s) - cmath.phase(c)) + 180) % 360 - 180
            if abs(abs(d) - 90) < 2:
                out.append(f"Magnitude is right but the angle is off by {d:+.0f}°: a stray or missing j "
                           "(e.g. jωL vs 1/(jωC), or sin vs cos reference).")
            elif abs(cmath.phase(s) + cmath.phase(c)) < 0.02:
                out.append("Magnitude is right but the angle has the wrong sign: a conjugate, or lead/lag mixed up.")
            else:
                out.append(f"Magnitude is right but the angle is off by {d:+.3g}°.")
    out.append("Find the first step where their work departs from the correct one before telling them the fix.")
    return " ".join(out)


# ── Reference sheets ─────────────────────────────────────────────────────────

REFERENCE = {
    "sign_conventions": """\
- Passive sign convention: current enters the + terminal → p = v·i is power ABSORBED; p < 0 means the element delivers power.
- KCL: currents leaving a node sum to zero. KVL: voltage drops around any closed loop sum to zero.
- The voltage across a current source and the current through a voltage source are set by the rest of the circuit, not by the source.
- Sum of power absorbed by every element = 0 — a quick check on any full solution.
- Dependent sources are never switched off (not in superposition, not when finding Rth).""",
    "nodal_mesh": """\
- Nodal: choose ground, write KCL at each of the N−1 other nodes with currents like (Va − Vb)/R.
- Voltage source between two non-reference nodes → supernode: one KCL around both nodes + the constraint Va − Vb = Vs.
- Voltage source from a node to ground fixes that node voltage; no KCL needed there.
- Mesh: one clockwise current per window; a resistor shared by meshes 1 and 2 carries I1 − I2 (seen from mesh 1).
- Current source on the outer edge fixes that mesh current; shared by two meshes → supermesh plus I1 − I2 = Is.
- Dependent sources: write their controlling variable in terms of the unknowns (node voltages or mesh currents).
- Pitfall: writing KCL at the reference node too gives a dependent (singular) system.""",
    "thevenin_norton": """\
- Vth = open-circuit voltage at the port; In = short-circuit current; Rth = Vth / In.
- Only independent sources: turn them off (V → short, I → open) and combine resistances.
- With dependent sources: keep them, turn independent sources off, apply a test source: Rth = Vtest / Itest. Rth can be negative.
- Source transformation: Vs in series with R ⇔ Is = Vs/R in parallel with R (current arrow points to the + side).
- Max power transfer: RL = Rth, Pmax = Vth²/(4Rth). AC: ZL = Zth*, Pmax = |Vth|²/(8Rth) for peak phasors, |Vth|²/(4Rth) for RMS.""",
    "opamps": """\
- Ideal op-amp with negative feedback: V+ = V−, and no current flows into either input.
- Inverting: Vo = −(Rf/Rin)·Vin. Non-inverting: Vo = (1 + Rf/Rg)·Vin. Follower: Vo = Vin.
- Summing (inverting): Vo = −Rf·(V1/R1 + V2/R2 + ...). Difference amp with R2/R1 matched: Vo = (R2/R1)(V2 − V1).
- Integrator: Vo = −(1/RC)∫Vin dt. Differentiator: Vo = −RC·dVin/dt.
- Write KCL at the INPUT nodes, never at the output node (the output supplies whatever current is needed).
- Output can't exceed the supply rails: if the formula gives more, the op-amp saturates.""",
    "first_order": """\
- τ = Rth·C or L/Rth, where Rth is the Thevenin resistance seen by the capacitor or inductor (sources off).
- x(t) = x(∞) + [x(0+) − x(∞)]·e^(−t/τ) for t ≥ 0 (shift t → t − t0 for a later switch).
- Continuous across a switch: capacitor VOLTAGE and inductor CURRENT. Capacitor current and inductor voltage can jump.
- DC steady state (t = 0− and t = ∞): capacitor = open circuit, inductor = short circuit.
- i_C = C·dv/dt, v_L = L·di/dt. Energy: ½CV², ½LI².
- After 5τ the response is within 1% of final.""",
    "second_order": """\
- Series RLC: α = R/(2L). Parallel RLC: α = 1/(2RC). Both: ω0 = 1/√(LC).
- s1,2 = −α ± √(α² − ω0²). α > ω0 overdamped, α = ω0 critically damped, α < ω0 underdamped with ωd = √(ω0² − α²).
- Overdamped: A1e^(s1 t) + A2e^(s2 t). Critical: (A1 + A2 t)e^(−αt). Underdamped: e^(−αt)(B1 cos ωd t + B2 sin ωd t). Add x(∞) for a step.
- Constants come from x(0+) and dx/dt(0+): dv_C/dt(0+) = i_C(0+)/C, di_L/dt(0+) = v_L(0+)/L.
- ζ = α/ω0, Q = ω0/(2α).""",
    "phasors_ac": """\
- v(t) = Vm·cos(ωt + φ) ⇔ V = Vm∠φ. Convert sines first: sin(x) = cos(x − 90°).
- Z_R = R, Z_L = jωL, Z_C = 1/(jωC) = −j/(ωC). ω = 2πf in rad/s — never plug Hz in for ω.
- Series impedances add; parallel: 1/Z = Σ 1/Zi. Nodal, mesh, Thevenin all work with complex numbers.
- Inductor current lags its voltage by 90°; capacitor current leads by 90° (ELI the ICE man).
- Adding phasors: convert to rectangular, add, convert back. Multiply/divide in polar.
- Converting back: v(t) = |V|·cos(ωt + ∠V).""",
    "ac_power": """\
- Peak phasors: S = ½·V·I*. RMS phasors: S = V·I*. Sinusoid: Vrms = Vm/√2.
- P = Re(S) watts, Q = Im(S) VAR, |S| VA. P = Vrms·Irms·cos(θv − θi).
- pf = cos(θv − θi): lagging when current lags (inductive, Q > 0), leading when it leads (capacitive, Q < 0).
- Resistors absorb only P, inductors absorb +Q, capacitors absorb −Q (supply Q).
- PF correction with a parallel capacitor: C = (Q_old − Q_new)/(ω·Vrms²).""",
    "frequency_response": """\
- H(jω) = Vout/Vin. RC low-pass: H = 1/(1 + jωRC), ωc = 1/(RC), fc = 1/(2πRC). RL low-pass: ωc = R/L.
- High-pass RC: H = jωRC/(1 + jωRC). At cutoff |H| = 1/√2 (−3 dB), phase ∓45°.
- dB = 20·log10|H| for voltage/current ratios, 10·log10 for power ratios.
- Bode: each pole −20 dB/decade and −90° total phase; each zero +20 dB/decade and +90°.
- Series RLC band-pass across R: ω0 = 1/√(LC), BW = R/L, Q = ω0/BW; ωc1·ωc2 = ω0².""",
    "laplace": """\
- Z_R = R, Z_L = sL, Z_C = 1/(sC).
- Initial conditions: V_L(s) = sL·I_L(s) − L·i_L(0−); I_C(s) = sC·V_C(s) − C·v_C(0−).
- Transfer function H(s) = Y(s)/X(s) with zero initial conditions; impulse response h(t) = L⁻¹{H(s)}.
- Final value: f(∞) = lim s→0 of s·F(s) (only if all poles of s·F(s) are in the left half-plane). Initial value: f(0+) = lim s→∞ of s·F(s).
- Partial fractions; a pair of poles −α ± jβ gives e^(−αt)(A cos βt + B sin βt).""",
    "diodes": """\
- Constant-voltage model: ON → 0.7 V drop (silicon) with current flowing anode→cathode ≥ 0; OFF → open with V_AK < 0.7 V.
- Method: assume a state for each diode, solve the linear circuit, then CHECK the assumption (ON needs I ≥ 0, OFF needs V_AK < Von). Wrong → flip and redo.
- Ideal model: Von = 0. Shockley: I = Is(e^(V/(n·VT)) − 1), VT ≈ 25.9 mV at 27 °C.
- Zener in breakdown holds Vz with current flowing cathode→anode.""",
}


def reference(topic: str) -> str:
    key = re.sub(r"[^a-z]+", "_", str(topic or "").lower()).strip("_")
    aliases = {"ac": "phasors_ac", "phasor": "phasors_ac", "phasors": "phasors_ac", "power": "ac_power",
               "opamp": "opamps", "op_amp": "opamps", "op_amps": "opamps", "thevenin": "thevenin_norton",
               "norton": "thevenin_norton", "nodal": "nodal_mesh", "mesh": "nodal_mesh", "rc": "first_order",
               "rl": "first_order", "transient": "first_order", "rlc": "second_order", "filters": "frequency_response",
               "bode": "frequency_response", "signs": "sign_conventions", "diode": "diodes"}
    key = aliases.get(key, key)
    if key in REFERENCE:
        return REFERENCE[key]
    return "Topics: " + ", ".join(REFERENCE)


# ── Entry point ──────────────────────────────────────────────────────────────

def _show(player, title: str, text: str) -> None:
    if player is None:
        return
    try:
        player.show_content(title, text)
    except Exception:
        pass


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action", "")).strip().lower()
    try:
        if action == "calc":
            result = calc(str(p.get("expression", "")), str(p.get("variables", "")))
            _show(player, "CALCULATION", result)
            return result
        if action == "solve_circuit":
            result = solve_circuit(str(p.get("netlist", "")), str(p.get("freq_hz", "") or ""),
                                   str(p.get("omega", "") or ""), str(p.get("port", "") or ""),
                                   str(p.get("phasors", "") or "peak"), str(p.get("variables", "")))
            _show(player, "CIRCUIT SOLUTION", "NETLIST\n" + str(p.get("netlist", "")).strip() + "\n\n" + result)
            return result + ("\nTell the user which values you used if you read the circuit from the screen, "
                             "so they can catch a misread.")
        if action == "solve_equations":
            result = solve_equations(str(p.get("equations", "")), str(p.get("variables", "")))
            _show(player, "EQUATIONS", str(p.get("equations", "")).strip() + "\n\n" + result)
            return result
        if action == "check":
            return check(str(p.get("answer", "")), str(p.get("correct", "")),
                         str(p.get("tolerance_percent", "") or ""), str(p.get("variables", "")))
        if action == "reference":
            return reference(str(p.get("topic", "")))
    except (ValueError, NameError, ZeroDivisionError, OverflowError, TypeError) as exc:
        return f"Math error: {exc}"
    return "Use action calc, solve_circuit, solve_equations, check or reference."
