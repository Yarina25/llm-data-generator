# -*- coding: utf-8 -*-
"""
TestData.py 端到端自检脚本

做三件事：
  1. 用当前解释器拉起 TestData:app（自动挑空闲端口，不占用你常用的 8000）
  2. 真实 POST 一次 /api/generate_data
  3. 拿返回的 schema 反过来校验 data —— 枚举是否越界、类型是否对、min/max 是否越界、unique 是否重复、条数是否一致

改提示词、换模型之后直接重跑本脚本即可，不用手动开服务。

用法（在脚本所在目录执行）：
    python test_e2e.py
    python test_e2e.py --prompt "生成5个中文订单，字段：订单号、客户、金额"
    python test_e2e.py --runs 3          # 连跑 3 次，抓模型偶发不返回 JSON 的情况
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MODULE = "TestData:app"
PATH = "/api/generate_data"
DEFAULT_PROMPT = (
    "生成8个中文员工，字段：姓名、部门、年龄、薪资，"
    "部门从技术部/市场部/财务部里选，薪资5000-20000"
)

# 与你提示词里明确写死的约束一一对应。
# 关键：这是「用户预期」，不是「模型自己给的」，模型漏掉约束时会被抓出来。
# 注意：默认提示词没有规定年龄范围，所以这里不校验年龄（模型自行给出 22~60 属合理推断）；
# 需要卡年龄时自己加一段，例如：--expect "部门=enum:技术部,市场部,财务部;薪资=range:5000,20000;年龄=range:18,60"
DEFAULT_EXPECT = "部门=enum:技术部,市场部,财务部;薪资=range:5000,20000"


def _utf8_stdout():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def pick_port():
    """让系统分配一个空闲端口，避免和已占用的 8000 冲突"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_up(url, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.4)
    return False


def flatten(field):
    """兼容约束嵌套在 constraints 里、或直接平铺在字段上两种写法"""
    flat = dict(field)
    c = flat.pop("constraints", None)
    if isinstance(c, dict):
        for k, v in c.items():
            flat.setdefault(k, v)
    return flat


def validate(schema, data):
    """按 schema 反向校验 data，返回问题列表（空列表=全部通过）"""
    problems = []
    fields = [flatten(f) for f in (schema.get("fields") or [])]
    names = [f.get("name") for f in fields]

    count = schema.get("count")
    if isinstance(count, int) and count != len(data):
        problems.append(f"条数不符：schema.count={count}，实际返回 {len(data)} 条")

    for i, row in enumerate(data):
        missing = [n for n in names if n not in row]
        extra = [k for k in row if k not in names]
        if missing:
            problems.append(f"#{i} 缺字段 {missing}")
        if extra:
            problems.append(f"#{i} 多出未定义字段 {extra}")

        for f in fields:
            n = f.get("name")
            if n not in row:
                continue
            v = row[n]

            if "enum" in f and v not in f["enum"]:
                problems.append(f"#{i} 字段「{n}」={v!r} 越出枚举 {f['enum']}")
            if "value" in f and v != f["value"]:
                problems.append(f"#{i} 字段「{n}」={v!r} 应为固定值 {f['value']!r}")

            t = str(f.get("type", "")).lower()
            if t in ("number", "integer", "int", "float"):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    problems.append(f"#{i} 字段「{n}」={v!r} 不是数值")
                else:
                    if "min" in f and v < f["min"]:
                        problems.append(f"#{i} 字段「{n}」={v} 小于下限 {f['min']}")
                    if "max" in f and v > f["max"]:
                        problems.append(f"#{i} 字段「{n}」={v} 超过上限 {f['max']}")
                    if t in ("integer", "int") and not isinstance(v, int):
                        problems.append(f"#{i} 字段「{n}」={v!r} 应为整数")
            elif t == "string" and not isinstance(v, str):
                problems.append(f"#{i} 字段「{n}」={v!r} 不是字符串")

    for f in fields:
        if f.get("unique"):
            vals = [row.get(f["name"]) for row in data]
            dups = sorted({x for x in vals if x is not None and vals.count(x) > 1}, key=str)
            if dups:
                problems.append(f"字段「{f['name']}」应为唯一值，出现重复：{dups[:5]}")

    return problems


def run_once(port, prompt, timeout):
    body = json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{PATH}",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def tail(path, limit=1600):
    """读服务端日志尾部，用于失败时给出真正的错误"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[-limit:]
    except Exception:
        return "(无日志)"


def parse_expect(spec):
    """解析 --expect，格式：字段=enum:值1,值2;字段=range:下限,上限"""
    exp = {}
    for part in (spec or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, rest = part.partition("=")
        kind, _, val = rest.partition(":")
        name, kind = name.strip(), kind.strip().lower()
        if kind == "enum":
            exp[name] = ("enum", [v.strip() for v in val.split(",") if v.strip()])
        elif kind == "range":
            lo, _, hi = val.partition(",")
            exp[name] = ("range", (float(lo), float(hi)))
        else:
            exp[name] = ("unknown", val)
    return exp


def check_expect(schema, data, exp):
    """对照用户原话里的约束做硬校验 —— 模型漏写约束时这里会直接报错"""
    problems = []
    fields = {}
    for raw in schema.get("fields") or []:
        f = flatten(raw)
        fields[f.get("name")] = f

    for name, (kind, v) in exp.items():
        f = fields.get(name)
        if f is None:
            problems.append(f"schema 里根本没有字段「{name}」")
            continue

        sample = [r.get(name) for r in data[:3]]

        if kind == "enum":
            if "enum" not in f:
                problems.append(f"schema 未输出「{name}」的 enum（模型漏了约束），实际值样例 {sample}")
            elif sorted(f["enum"]) != sorted(v):
                problems.append(f"schema 的「{name}」enum 与预期不符：{f['enum']}，预期 {v}")
            for i, r in enumerate(data):
                if r.get(name) not in v:
                    problems.append(f"#{i} 字段「{name}」={r.get(name)!r} 不在预期枚举 {v}")

        elif kind == "range":
            lo, hi = v
            if "min" not in f or "max" not in f:
                problems.append(f"schema 未输出「{name}」的 min/max（模型漏了约束），实际值样例 {sample}")
            for i, r in enumerate(data):
                val = r.get(name)
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    problems.append(f"#{i} 字段「{name}」={val!r} 不是数值")
                elif not (lo <= val <= hi):
                    problems.append(f"#{i} 字段「{name}」={val} 超出预期 {lo:g}~{hi:g}")

    return problems


def main():
    _utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--runs", type=int, default=1, help="连续跑几次，用于抓偶发失败")
    ap.add_argument("--port", type=int, default=0, help="0=自动挑空闲端口")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--delay", type=float, default=0.0, help="每次请求之间的间隔秒数，用来避开免费额度限流")
    ap.add_argument(
        "--expect",
        default=None,
        help="对照提示词硬校验，格式：字段=enum:值1,值2;字段=range:下限,上限（用默认提示词时自动套用内置预期）",
    )
    args = ap.parse_args()

    if args.expect is None:
        expect_spec = DEFAULT_EXPECT if args.prompt == DEFAULT_PROMPT else ""
    else:
        expect_spec = args.expect
    exp = parse_expect(expect_spec)

    port = args.port or pick_port()
    base = f"http://127.0.0.1:{port}"

    # 服务端输出重定向到文件（用管道会被缓冲区写满而卡死）
    logdir = tempfile.mkdtemp(prefix="e2e_")
    logout = os.path.join(logdir, "server.out.log")
    logerr = os.path.join(logdir, "server.err.log")

    with open(logout, "wb") as fo, open(logerr, "wb") as fe:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", MODULE, "--host", "127.0.0.1", "--port", str(port)],
            stdout=fo,
            stderr=fe,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    failed = 0
    skipped = 0
    try:
        if not wait_up(base + "/docs"):
            print("✗ 服务启动失败，下面是服务端输出：")
            print(tail(logerr))
            return 2
        print(f"✓ 服务已启动 {base}  (pid={proc.pid})")

        for n in range(1, args.runs + 1):
            tag = f"[第 {n} 次]" if args.runs > 1 else ""
            try:
                status, payload = run_once(port, args.prompt, args.timeout)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                if e.code == 429:
                    print(f"⚠ {tag} HTTP 429 上游限流，本次跳过（不计入失败）")
                    skipped += 1
                    time.sleep(max(args.delay, 20))
                    continue
                failed += 1
                print(f"✗ {tag} HTTP {e.code}")
                if body:
                    print("  响应体:", body)
                print("  服务端日志尾部：")
                print(tail(logerr))
                continue
            except Exception as e:
                failed += 1
                print(f"✗ {tag} 请求异常 {type(e).__name__}: {e}")
                continue

            schema, data = payload.get("schema", {}), payload.get("data", [])
            problems = validate(schema, data) + check_expect(schema, data, exp)

            print(f"✓ {tag} HTTP {status} | 字段={[f.get('name') for f in schema.get('fields', [])]} "
                  f"| 条数={len(data)}/{schema.get('count')}")
            print("  样例:", json.dumps(data[:2], ensure_ascii=False))
            if problems:
                failed += 1
                print(f"  ✗ 校验发现 {len(problems)} 处问题：")
                for p in problems[:12]:
                    print("     -", p)
            else:
                print("  ✓ 校验通过：枚举、类型、min/max、unique、条数均合规")

            if args.delay and n < args.runs:
                time.sleep(args.delay)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        if failed:
            print("服务已停止（失败日志保留在 " + logdir + "）")
        else:
            print("服务已停止")
            shutil.rmtree(logdir, ignore_errors=True)

    if failed:
        print(f"\n结果：{failed}/{args.runs} 次未通过" + (f"（另有 {skipped} 次因限流跳过）" if skipped else ""))
        return 1
    ok = args.runs - skipped
    print(f"\n结果：{ok}/{ok} 次全部通过" + (f"（另有 {skipped} 次因限流跳过）" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
