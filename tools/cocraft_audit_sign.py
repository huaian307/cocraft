# -*- coding: utf-8 -*-
r"""★审计导出的**非对称签名**（复用系统自带的 OpenSSH，自己不实现密码学）

用途：把 `GET /audit/export` 导出的文件签一份，交给别人时对方能**离线验证「这份没被改过」**。
免费版也有链（哈希链），签名是"给第三方看"的那一层 —— 见 `docs/审计-方案.md` §8。

⚠ 为什么用 `ssh-keygen` 而不是自己搓 RSA/Ed25519：
   - 纯标准库没有非对称签名，自己实现容易悄悄写错（而这种东西写错=白签）；
   - `ssh-keygen -Y sign/verify` 是 OpenSSH 8.0+ 的标准功能（Windows 10+ 自带 OpenSSH 客户端），
     签名格式（SSHSIG）在 Linux / macOS 上同样能验 —— 甲方机器上也能复核。
   - 私钥**不进仓库、不进 `runtime/state/`**：默认放 `%USERPROFILE%\.cocraft\audit-key`。

用法：
    :: 1) 只做一次：生成密钥
    python tools\cocraft_audit_sign.py keygen

    :: 2) 导出（面板或 curl）之后签名
    python tools\cocraft_audit_sign.py sign cocraft-audit-2026-10-24T13-00-00-000Z.json

    :: 3) 对方拿到「导出文件 + .sig + 公钥」后离线验证
    python tools\cocraft_audit_sign.py verify export.json --pub cocraft-audit.pub

退出码：0 = 通过 / 1 = 失败（签名不对 / 文件被改） / 2 = 环境不支持（没装 ssh-keygen）
输出纯文本（`[OK]` / `[BAD]`），中文用 UTF-8。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NS = "cocraft"                                  # SSHSIG 命名空间（签/验必须一致）
DEFAULT_IDENTITY = "cocraft"
try:
    sys.stdout.reconfigure(errors="replace")    # 控制台是 GBK，别因为一个字符崩掉
except Exception:  # noqa: BLE001
    pass


def ssh_keygen() -> str:
    """找 ssh-keygen：PATH 优先，其次 Windows 自带那个（不在 PATH 时也能用）。"""
    p = shutil.which("ssh-keygen") or shutil.which("ssh-keygen.exe")
    if p:
        return p
    for cand in (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "OpenSSH", "ssh-keygen.exe"),
                 "/usr/bin/ssh-keygen", "/usr/local/bin/ssh-keygen"):
        if os.path.isfile(cand):
            return cand
    return ""


def default_key() -> str:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~") or "."
    return os.path.join(home, ".cocraft", "audit-key")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(args, stdin_path: str = "") -> tuple:
    """跑 ssh-keygen → (returncode, 输出文本)。stderr 也收（它把进度写在 stderr）。"""
    fh = None
    try:
        if stdin_path:
            fh = open(stdin_path, "rb")
            stdin = fh
        else:
            stdin = subprocess.DEVNULL
        p = subprocess.run(args, stdin=stdin, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=60)
    except OSError as exc:
        return 127, "%s: %s" % (type(exc).__name__, exc)
    except subprocess.TimeoutExpired:
        return 124, "ssh-keygen 超时（60s）"
    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace").strip()


def cmd_keygen(args) -> int:
    ssh = ssh_keygen()
    if not ssh:
        print("[BAD] 没找到 ssh-keygen（需要 OpenSSH 8.0+；Windows 10+ 自带客户端）")
        return 2
    key = os.path.abspath(args.key or default_key())
    if os.path.exists(key):
        print("[OK] 密钥已存在，不覆盖：%s" % key)
        print("     公钥：%s.pub" % key)
        return 0
    os.makedirs(os.path.dirname(key) or ".", exist_ok=True)
    code, out = _run([ssh, "-q", "-t", "ed25519", "-N", "", "-C", DEFAULT_IDENTITY,
                      "-f", key])
    if code != 0:
        print("[BAD] 生成密钥失败（exit %s）：%s" % (code, out))
        return 1
    pub = key + ".pub"
    print("[OK] 已生成密钥对（私钥只在本机，别外传）：")
    print("     私钥：%s" % key)
    print("     公钥：%s" % pub)
    try:
        with open(pub, "r", encoding="utf-8") as fh:
            print("     公钥内容：%s" % fh.read().strip())
    except OSError:
        pass
    return 0


def cmd_sign(args) -> int:
    ssh = ssh_keygen()
    if not ssh:
        print("[BAD] 没找到 ssh-keygen（需要 OpenSSH 8.0+）")
        return 2
    target = os.path.abspath(args.file)
    if not os.path.isfile(target):
        print("[BAD] 找不到文件：%s" % target)
        return 1
    key = os.path.abspath(args.key or default_key())
    if not os.path.isfile(key):
        print("[BAD] 找不到私钥：%s" % key)
        print("      先生成一次：python tools\\cocraft_audit_sign.py keygen")
        return 1
    sig = target + ".sig"
    try:                                        # 旧签名会让 ssh-keygen 问 y/n → 先删
        if os.path.exists(sig):
            os.remove(sig)
    except OSError:
        pass
    code, out = _run([ssh, "-Y", "sign", "-f", key, "-n", NS, target])
    if code != 0 or not os.path.exists(sig):
        print("[BAD] 签名失败（exit %s）：%s" % (code, out))
        return 1
    print("[OK] 已签名：%s" % sig)
    print("     命名空间：%s" % NS)
    print("     文件 SHA-256：%s" % _sha256(target))
    print("     把这三个文件一起给对方：导出文件 + .sig + 公钥（%s.pub）" % key)
    print("     对方验证：python tools\\cocraft_audit_sign.py verify <导出文件> --pub <公钥>")
    return 0


def _allowed_signers(pub_path: str, identity: str) -> str:
    """把公钥转成 ssh-keygen 要的 allowed_signers（`身份 类型 base64`）。"""
    with open(pub_path, "r", encoding="utf-8") as fh:
        parts = fh.read().split()
    if len(parts) < 2:
        raise ValueError("公钥读不懂（应该以 ssh-ed25519 / ssh-rsa 开头）")
    return "%s %s %s\n" % (identity, parts[0], parts[1])


def cmd_verify(args) -> int:
    ssh = ssh_keygen()
    if not ssh:
        print("[BAD] 没找到 ssh-keygen（需要 OpenSSH 8.0+）")
        return 2
    target = os.path.abspath(args.file)
    if not os.path.isfile(target):
        print("[BAD] 找不到文件：%s" % target)
        return 1
    sig = os.path.abspath(args.sig or (target + ".sig"))
    if not os.path.isfile(sig):
        print("[BAD] 找不到签名：%s" % sig)
        return 1
    pub = os.path.abspath(args.pub or (args.key or default_key()) + ".pub")
    if not os.path.isfile(pub):
        print("[BAD] 找不到公钥：%s（用 --pub 指定）" % pub)
        return 1
    identity = str(args.identity or DEFAULT_IDENTITY)
    try:
        allowed = _allowed_signers(pub, identity)
    except (OSError, ValueError) as exc:
        print("[BAD] %s" % exc)
        return 1
    tmpf = os.path.join(tempfile.mkdtemp(prefix="cocraft_sig_"), "allowed_signers")
    with open(tmpf, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(allowed)
    code, out = _run([ssh, "-Y", "verify", "-f", tmpf, "-I", identity, "-n", NS,
                      "-s", sig], stdin_path=target)
    print("文件      ：%s" % target)
    print("签名      ：%s" % sig)
    print("公钥      ：%s" % pub)
    print("文件 SHA-256：%s" % _sha256(target))
    if code == 0:
        print("[OK] 签名有效（%s）—— 这份导出在签名之后没被改过" % (out or "Good signature"))
        return 0
    print("[BAD] 签名校验失败：%s" % (out or "exit %s" % code))
    print("      常见原因：文件被改过 / 签名与文件不配对 / 公钥不是签名那把")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="cocraft 审计导出签名（复用 OpenSSH）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keygen", help="生成一次性密钥对（默认 ~/.cocraft/audit-key）")
    k.add_argument("--key", default="", help="私钥路径（默认 %s）" % default_key())
    k.set_defaults(func=cmd_keygen)

    s = sub.add_parser("sign", help="给导出文件签名（生成 <文件>.sig）")
    s.add_argument("file")
    s.add_argument("--key", default="", help="私钥路径（默认 %s）" % default_key())
    s.set_defaults(func=cmd_sign)

    v = sub.add_parser("verify", help="验证签名（对方拿到导出 + .sig + 公钥后跑这个）")
    v.add_argument("file")
    v.add_argument("--pub", default="", help="公钥路径（默认私钥路径 + .pub）")
    v.add_argument("--sig", default="", help="签名路径（默认 <文件>.sig）")
    v.add_argument("--key", default="", help="私钥路径（只用来推导 .pub）")
    v.add_argument("--identity", default=DEFAULT_IDENTITY, help="allowed_signers 里的身份名")
    v.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
