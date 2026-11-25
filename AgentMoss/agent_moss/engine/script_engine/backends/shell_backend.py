"""脚本分析引擎 — ShellBackend

shell（bash/sh/zsh）脚本的静态解析后端，产出 IR。

比旧版 branch_aware_preprocess 三点升级：
1. **case 支持任意 selector**：`case $1 in` / `case $MODE in` / `case ${1} in`。
   只要 selector 变量能被 entry_args 绑定，就用绑定的值选分支。
2. **if 支持字符串相等条件**：`if [ "$1" = "clean" ]` / `[ "$MODE" == "x" ]` /
   `[[ "$1" = clean ]]`。只识别"字面量相等"这类可静态判定的条件。
3. **循环可达**：`while`/`for` 循环体视为可达（保守——循环次数运行期未知，
   不能证明不可达，故保留，避免漏报）。

子脚本调用提取升级：`bash child.sh cleanup` 提取 (child.sh, ["cleanup"])，
供 RecursiveTracker 做跨脚本参数传递（修复旧版子调用参数丢失 bug）。

性能：纯正则 + 行扫描，单脚本 < 40ms（与旧版 preprocess 量级一致）。
双端（Python/TS）各实现一份，行为逐 case 对齐。
"""

import re

from ..ir import ScriptCall, ScriptIR, scan_fingerprint
from .base import LanguageBackend
from .symbolic import CASE_HEADER_RE, case_syntax_lines, parse_case_statement, select_case_branch


# ==================== 参数绑定 ====================
# 把 entry_args 绑到 $1/$2/${1}/${@}/$*，支持 ${1:-default}


def bind_positional_args(content: str, args: list[str]) -> str:
    """把脚本位置参数 $1/$2/${1}/${@}/${*} 替换为实际参数值。"""
    if not args:
        return content
    all_args = " ".join(args)

    def _repl_num(kind: str, default_val: str | None = None) -> str:
        idx = int(kind)
        if 1 <= idx <= len(args):
            return args[idx - 1]
        if default_val is not None:
            return default_val
        return ""

    # ${n} / ${n:-default} / ${@} / ${*}
    content = re.sub(
        r"\$\{(\d+|@|\*)(?::?[-]?([^}]*))?\}",
        lambda m: (
            all_args
            if m.group(1) in ("@", "*")
            else _repl_num(m.group(1), m.group(2) if m.group(2) is not None else None)
        ),
        content,
    )
    # 裸 $1 $2（不含 ${...}）
    content = re.sub(r"\$(\d+)", lambda m: _repl_num(m.group(1)), content)
    content = re.sub(r"\$[@*]", lambda m: all_args, content)
    return content


# ==================== case 分支解析 ====================


def _find_selector_value(selector: str, args: list[str] | None) -> str | None:
    """解析 case selector（$1 / $MODE / ${1}），从 args 中找到绑定值。

    只支持位置参数：$1 → args[0]，$2 → args[1]，$@ → 所有参数。
    命名字符串（$MODE）不识别（无环境变量上下文），返回 None（fail-closed）。
    """
    if not args:
        return None
    m = re.match(r"^\$\{?(\d+)\}?$", selector)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(args):
            return args[idx - 1]
        return None  # 位置越界，未知
    return None  # $MODE 等命名变量不可静态绑定


def parse_case(
    content: str, args: list[str] | None, *, diagnostics: dict | None = None,
) -> str | None:
    """用已知的入口参数选择 case 分支；无法确定时交给 LiteSE。"""
    if not args or not CASE_HEADER_RE.search(content):
        return None
    lines = content.split("\n")
    syntax_lines = case_syntax_lines(content)
    for index, line in enumerate(syntax_lines):
        if CASE_HEADER_RE.match(line):
            break
    else:
        return None

    case = parse_case_statement(lines, syntax_lines, index)
    if case is None:
        return None
    value = _find_selector_value(case.selector, args)
    if value is None:
        return None
    selection = select_case_branch(case, value)
    if selection is None:
        return None

    branch, kind = selection
    if diagnostics is not None:
        diagnostics["p1_case"] = kind
    end = index + case.line.count("\n") + 1
    return "\n".join([*lines[:index], case.branches[branch][1], *lines[end:]])


# ==================== if 分支解析 ====================


def _eval_if_condition(cond: str, args: list[str] | None) -> bool | None:
    """求值 if 的字符串条件；无法判定返回 None。

    支持：
      [ "$1" = "clean" ] / [ "$1" == "clean" ] / [ "$1" != "x" ]
      [ "$1" = bak* ]（glob 通配符）
      [[ "$1" =~ ^ba.* ]]（正则）
      [ -f /tmp/x ]（文件存在性 → None，运行期）
    条件变量里 $1 替换为 args[0] 后比较；操作符按最长优先匹配，`=~` 不会被 `=` 截断。
    """
    if not args:
        return None
    inner = re.sub(r"^\[\[?|\]\]?$", "", cond).strip()
    inner = inner.replace('"', "").replace("'", "")
    m = re.search(r"(.*?)\s*(=~|==|!=|=)\s*(.*)", inner)
    if not m:
        return None  # 非字符串条件（-f 等）无法静态判定
    op = m.group(2)
    left, right = m.group(1).strip(), m.group(3).strip()

    def _resolve(tok: str) -> str | None:
        rm = re.match(r"^\$(\d+)$", tok)
        if rm:
            idx = int(rm.group(1))
            return args[idx - 1] if 1 <= idx <= len(args) else None
        if tok.startswith("$"):
            return None  # $MODE 命名变量不可绑定
        return tok

    lv, rv = _resolve(left), _resolve(right)
    if lv is None or rv is None:
        return None
    if op == "=~":
        if rv.startswith("/") and rv.endswith("/") and len(rv) > 1:
            rv = rv[1:-1]
        try:
            return bool(re.search(rv, lv))
        except re.error:
            return None
    if "*" in rv or "?" in rv or "[" in rv:
        import fnmatch
        matched = fnmatch.fnmatchcase(lv, rv)
    else:
        matched = lv == rv
    return (not matched) if op == "!=" else matched


def parse_if(
    content: str, args: list[str] | None, *, diagnostics: dict | None = None,
) -> str | None:
    """解析 if [cond] then A else B fi，选可达分支。None = 无法静态判定。

    只支持 else 分支存在且条件可求值的形态；无法判定 → 保两条分支都可达
    （fail-open 方向，由 Scanner fail-closed 兜底 → 等价于全文本，不丢召回）。
    """
    m = re.search(r"\bif\s+([^\n;]+?)\s*;\s*then", content)
    if not m:
        return None
    cond = m.group(1).strip()
    truth = _eval_if_condition(cond, args)
    if truth is None:
        return None  # 不可静态判定 → 全文本（两分支都留）

    start = m.start()
    then_body_start = m.end()
    # then 到 else / elif / fi
    fi_pos = content.find("fi", then_body_start)
    if fi_pos == -1:
        return None
    segment = content[then_body_start:fi_pos]
    else_m = re.search(r"\belse\b", segment)
    if truth:
        chosen = segment if not else_m else segment[: else_m.start()]
    else:
        if not else_m:
            return None  # 无 else 且条件假 → 什么都不执行，但保守保整个（无法证明）
        chosen = segment[else_m.end():]

    prefix = content[:start]
    suffix = content[fi_pos + len("fi"):]
    if diagnostics is not None:
        diagnostics["p1_if"] = "then" if truth else "else"
    return f"{prefix}\n{chosen}\n{suffix}"


# ==================== 非执行行剔除（shebang / 整行注释） ====================

_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def strip_non_executable_lines(content: str) -> str:
    """剔除**不会执行**的行：首行 shebang 与整行注释，供 L1/L2/L3 扫描。

    为什么（2026-09-30 实测）：脚本内容会被套用 L1 规则（含银行场景的目录白名单），
    而 shebang 里的 `/bin/bash`、`/usr/bin/env` 以及注释里提到的路径都会被当成
    "真实文件访问" → 命中场景白名单规则 → **整脚本 Deny**。表现是「任何带 shebang
    的脚本，被 `bash x.sh` 调用就拦」—— 而客户沙箱里几乎每个运维脚本都带 shebang。
    注释同理：`# 见 /etc/passwd` 不是访问，不该拦。

    两个保守约束（都是防"剔出新漏报"）：
    1. 只剔**整行**注释（行首空白后可跟 `#`），不动行尾注释 —— 尾注释与代码同行，
       代码部分必须保留；
    2. **heredoc 体内不剔**：`cat <<EOF` 与 `EOF` 之间是数据（会被写进文件/传给命令），
       里面的 `# ...` 是内容不是注释，剔掉会漏掉其中的路径。
    3. shebang 只认**首行**，避免误伤 heredoc 或字符串里的 `#!`。
    """
    if not content:
        return content
    out: list[str] = []
    heredoc_end: str | None = None
    for i, line in enumerate(content.split("\n")):
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            out.append(line)
            continue
        m = _HEREDOC_RE.search(line)
        if m:
            heredoc_end = m.group(1)
        stripped = line.lstrip()
        if i == 0 and stripped.startswith("#!"):
            continue
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


# ==================== 循环体（保守可达） ====================



def _mark_loops_reachable(content: str) -> str:
    """循环体（while/for/until）全部保留（保守：循环执行次数未知，不可证明不可达）。"""
    # 标记循环结构已处理（不剔除任何内容），返回原样。
    # 这是保守决策：任何剔除都可能漏报（循环体可能在多次迭代中触发危险）。
    return content


# ==================== 子脚本调用提取（含参数） ====================

# bash x.sh param → (x.sh, [param])；整体覆盖含 |/引号的后缀参数
_CHILD_CALL_RE = re.compile(
    r"(?:^|\s|&&|\|\|)\s*"
    r"((?:/bin/)?bash|sh|zsh|(?:python|python3|node|ruby|perl|php|lua))\s+"
    r"""("[^"\n]+"|'[^'\n]+'|[^\s;|&]+)"""
    r"([^\n;|&]*?)(?=(?:;;&|;&|;;)[ \t]*(?:\#[^\n]*)?$|$)",
    re.MULTILINE | re.IGNORECASE,
)


def extract_calls_from_content(content: str) -> list[ScriptCall]:
    """提取脚本内容里的子脚本调用（含参数）。Python/Node 等解释器子调用也可识别。"""
    calls: list[ScriptCall] = []
    for m in _CHILD_CALL_RE.finditer(content):
        interp, path, rest = m.group(1), m.group(2).strip(), m.group(3).strip()
        if path.startswith(("'", '"')) and path[-1] == path[0]:
            path = path[1:-1]
        if path.startswith(("-", "$")) or "/dev/" in path:
            continue
        # 参数切分（剥引号，按空白/管道断）
        rest = re.split(r"[;|&<>]", rest)[0].strip()
        args = []
        for tm in re.finditer(r"""(["'])(.*?)\1|(\S+)""", rest):
            args.append(tm.group(2) if tm.group(1) else tm.group(3))
        calls.append(ScriptCall(path=path, args=args, interpreter=interp))
    return calls


# ==================== ShellBackend ====================


class ShellBackend(LanguageBackend):
    """shell(bash/sh/zsh) 脚本解析后端。"""

    language = "shell"

    ENTRY_PATTERNS = [
        r"(?:^|\s|&&|\|\|)\s*bash\s+(\S+)",
        r"(?:^|\s|&&|\|\|)\s*(?:/bin/)?sh\s+(\S+)",
        r"(?:^|\s|&&|\|\|)\s*zsh\s+(\S+)",
        r"\./(\S+)",
        r"(?:^|\s|&&|\|\|)\s*source\s+(\S+)",
    ]

    def parse(
        self, content: str, entry_args: list[str] | None, *, diagnostics: dict | None = None,
    ) -> ScriptIR:
        """解析 shell 脚本：P1 → P2/P3/P4 升级 → fail-closed 回退。

        逐级责任链：
          P1 基于入口参数匹配 case/if → 能选分支就用
          P2/P3/P4 符号环境执行器 → P1 处理不了时升级（变量拼接/内部变量/数值）
          都处理不了 → fail-closed 全文本（loop_marked）

        漏报防护（NEW-FN）：脚本含 shift / set -- / read / 函数定义 时，
        P1 用入口参数绑定 $1 剪枝不可靠——$1 语义可能已被改写，或函数内
        $1 是函数参数而非入口参数。此时跳过 P1 绑定，交给 symbolic
        （对不可建模行 NotModeled → 整体 fail-closed 全文本），宁可多拦不可漏。
        """
        if content is None:
            content = ""

        candidate = content
        resolved = False

        # 位置参数/变量绑定安全性检测：命中则跳过 P1 的 $1 绑定剪枝。
        # `set --` 后是空格，`--` 末尾不能用 \b（- 非词字符，后无词边界）
        _P1_UNSAFE_RE = re.compile(r"\bshift\b|\bset\s+--(?:\s|$)|[\s;|&]read\b|^\s*read\b|\(\)\s*\{|function\s+\w+")
        p1_safe = not _P1_UNSAFE_RE.search(content)

        if p1_safe:
            case_out = parse_case(candidate, entry_args, diagnostics=diagnostics)
            if case_out is not None and case_out != candidate:
                resolved = True
                candidate = case_out
            # case 尚未解析时，不能用 if 剪枝绕过 LiteSE 的 case 判定。
            if not CASE_HEADER_RE.search(candidate):
                if_out = parse_if(candidate, entry_args, diagnostics=diagnostics)
                if if_out is not None and if_out != candidate:
                    resolved = True
                    candidate = if_out

        # P2/P3/P4 升级：LiteSE 顺序执行器（只在 P1 未能裁剪时尝试，避免双实现）
        if diagnostics is not None:
            diagnostics.update(parser="shell_p1", p1_safe=p1_safe, child_extractor="regex")
        if not resolved:
            from .symbolic import symbolic_process
            symbolic_diagnostics = {} if diagnostics is not None else None
            sym_out = symbolic_process(content, entry_args, diagnostics=symbolic_diagnostics)
            if sym_out is not None and sym_out != content:
                resolved = True
                candidate = sym_out
            if diagnostics is not None and symbolic_diagnostics is not None:
                diagnostics["parser"] = "shell_litese" if resolved else "shell_full"
                diagnostics["symbolic"] = symbolic_diagnostics
                if not resolved:
                    diagnostics["fallback_reason"] = symbolic_diagnostics["fallback_reason"] or "no_pruning"
                if sym_out is not None:
                    length, digest = scan_fingerprint(sym_out)
                    symbolic_diagnostics.update(output_bytes=length, output_sha256=digest)

        # 剔除不会执行的行（shebang / 整行注释）—— 见函数 docstring：
        # 否则 shebang 里的 /bin/bash 会被当"访问白名单外路径"，整脚本被误拦。
        # 全文回退仍可绑定未被修改的入口参数，供子脚本提取使用。
        bound = bind_positional_args(candidate, entry_args or []) if p1_safe else candidate
        bound = strip_non_executable_lines(bound)

        # 循环体保守标记（保留，不剔除）
        loop_marked = _mark_loops_reachable(bound)

        # 子脚本调用提取
        calls = extract_calls_from_content(loop_marked)

        if resolved:
            ir = ScriptIR(
                reachable_content=loop_marked,
                control_flow_resolved=True,
                calls=calls,
                language=self.language,
            )
        else:
            # fail-closed：全文本
            ir = ScriptIR(
                reachable_content=loop_marked,  # 即全文（未裁剪）
                control_flow_resolved=False,
                calls=calls,
                language=self.language,
            )
        return ir
