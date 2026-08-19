from __future__ import annotations

from .config import DISCLAIMER


def _fmt_num(value, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def _fmt_big(value) -> str:
    if value is None:
        return "N/A"
    try:
        num = float(value)
        if abs(num) >= 1e8:
            return f"{num / 1e8:.2f}亿"
        if abs(num) >= 1e4:
            return f"{num / 1e4:.2f}万"
        return f"{num:.0f}"
    except Exception:
        return str(value)


def _trend(change) -> str:
    try:
        c = float(change)
        if c > 0:
            return "🔴↑"
        if c < 0:
            return "🟢↓"
    except Exception:
        pass
    return "⚪"


def format_result(quotes: list[dict], failures: list[dict], warnings: list[str]) -> str:
    lines: list[str] = []
    if quotes:
        title = "📈 行情查询结果" if len(quotes) > 1 else "📈 行情查询结果"
        lines.append(title)
        lines.append("")
        for q in quotes:
            trend = _trend(q.get("change"))
            name = q.get("name") or q.get("symbol")
            lines.append(f"{trend} **{name}**（{q.get('symbol')}，{q.get('market')}）")
            lines.append(
                f"现价：{_fmt_num(q.get('price'))} {q.get('currency') or ''}｜"
                f"涨跌：{_fmt_num(q.get('change'))}（{_fmt_num(q.get('pct_chg'))}%）"
            )
            lines.append(
                f"今开：{_fmt_num(q.get('open'))}｜最高：{_fmt_num(q.get('high'))}｜"
                f"最低：{_fmt_num(q.get('low'))}｜昨收：{_fmt_num(q.get('pre_close'))}"
            )
            if q.get("volume") is not None or q.get("amount") is not None:
                lines.append(f"成交量：{_fmt_big(q.get('volume'))}｜成交额：{_fmt_big(q.get('amount'))}")
            lines.append(f"数据源：{q.get('source') or 'unknown'}｜时间：{q.get('timestamp') or 'N/A'}" + ("｜缓存" if q.get("cached") else ""))
            if q.get("warnings"):
                lines.append("提示：" + "；".join(str(x) for x in q.get("warnings", [])))
            lines.append("")
    if failures:
        lines.append("⚠️ 未能查询的标的：")
        for f in failures:
            lines.append(f"- {f.get('input')}: {f.get('error')}")
        lines.append("")
    for warning in warnings:
        if warning and warning != DISCLAIMER:
            lines.append(f"提示：{warning}")
    if DISCLAIMER:
        lines.append(DISCLAIMER)
    return "\n".join(lines).strip()
