from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

SPEC = {'description': '科学计算器。支持 LaTeX 表达式输入，进行精确符号计算（化简、求解、微积分、矩阵等）。返回计算结果的 LaTeX 和纯文本形式，附带计算用时。',
 'input_schema': {'properties': {'expr': {'description': 'LaTeX 数学表达式，如 \\frac{3}{7} + '
                                                         '\\frac{2}{5} 或 \\int_0^1 x^2 dx',
                                          'type': 'string'},
                                 'mode': {'default': 'eval',
                                          'description': '计算模式：eval(求值，默认) / simplify(化简) / '
                                                         'solve(求解，需指定 var) / diff(微分) / '
                                                         'integrate(积分) / limit(极限) / expand(展开) / '
                                                         'factor(因式分解) / matrix(矩阵运算)',
                                          'type': 'string'},
                                 'raw': {'description': '如果 LaTeX 解析失败，可直接传 sympy 表达式字符串作为备选',
                                         'type': 'string'},
                                 'var': {'default': 'x',
                                         'description': '变量名（用于 solve/diff/integrate/limit），默认 x',
                                         'type': 'string'}},
                  'required': ['expr'],
                  'type': 'object'},
 'name': 'sci_calc'}


if __name__ == "__main__":
    import json, sys
    _input = json.loads(sys.stdin.read())
    def output(result): print(json.dumps(result, ensure_ascii=False)); sys.exit(0)
    def fail(error):
        # [AutoC 2026-05-31] Why: calculator failures should still have a readable
        # data.result field for model history. How: emit ok=false with data.result
        # and the original error text before exiting non-zero. Purpose: keep parse
        # and compute errors visible under the unified schema.
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False)); sys.exit(1)
    args = _input
    import sympy
    import time as _time
    from sympy import *
    from sympy.parsing.latex import parse_latex
    
    _t0 = _time.perf_counter()
    
    expr_str = args.get('expr', '')
    mode = args.get('mode', 'eval')
    var_name = args.get('var', 'x')
    raw = args.get('raw', '')
    
    try:
        var = symbols(var_name)
    except Exception:
        var = symbols('x')
    
    # Try parsing LaTeX first, fall back to raw sympy string
    parsed = None
    try:
        parsed = parse_latex(expr_str)
    except Exception as e:
        latex_err = str(e)
        if raw:
            try:
                parsed = sympify(raw)
            except Exception as e2:
                fail(f'LaTeX parse failed: {latex_err}; raw parse also failed: {e2}')
        else:
            # Try direct sympify on the expr
            try:
                parsed = sympify(expr_str)
            except Exception:
                fail(f'LaTeX parse failed: {latex_err}. Try passing a raw sympy expression.')
    
    if parsed is None:
        fail('Could not parse expression')
    
    def _ms():
        return round((_time.perf_counter() - _t0) * 1000, 2)

    def _emit_calc(value, latex_value=None, **extra):
        # [AutoC 2026-05-31] Why: the old calculator used the key "result" for the
        # computed value, but the unified schema reserves data.result for readable
        # text. How: store the computed value as data.value, keep LaTeX separately,
        # and compose data.result from both. Purpose: avoid field-name conflict
        # while preserving precise calculation output.
        latex_str = latex_value if latex_value is not None else sympy.latex(value)
        value_str = value if isinstance(value, str) else str(value)
        data = {"result": f"{latex_str} = {value_str}", "value": value_str, "latex": latex_str, "elapsed_ms": _ms()}
        data.update(extra)
        output({"ok": True, "data": data})
    
    try:
        if mode == 'eval':
            result = nsimplify(parsed)
            try:
                numeric = float(result.evalf())
            except Exception:
                numeric = str(result.evalf())
            result_latex = sympy.latex(result)
            _emit_calc(str(result), result_latex, numeric=numeric)
    
        elif mode == 'simplify':
            result = simplify(parsed)
            _emit_calc(result)
    
        elif mode == 'solve':
            result = solve(parsed, var)
            result_strs = [str(r) for r in result]
            result_latex = [sympy.latex(r) for r in result]
            _emit_calc(result_strs, result_latex, solutions=result_strs)
    
        elif mode == 'diff':
            result = diff(parsed, var)
            _emit_calc(result)
    
        elif mode == 'integrate':
            result = integrate(parsed, var)
            _emit_calc(result)
    
        elif mode == 'limit':
            result = limit(parsed, var, 0)
            _emit_calc(result)
    
        elif mode == 'expand':
            result = expand(parsed)
            _emit_calc(result)
    
        elif mode == 'factor':
            result = factor(parsed)
            _emit_calc(result)
    
        elif mode == 'matrix':
            result = simplify(parsed)
            _emit_calc(result)
    
        else:
            result = simplify(parsed)
            _emit_calc(result)
    
    except Exception as e:
        fail(f'Computation error ({_ms()}ms): {e}')
