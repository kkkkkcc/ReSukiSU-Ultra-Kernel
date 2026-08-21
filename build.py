#!/usr/bin/env python3
"""
Tesla_Kernel 构建器 (GKI 2.0) — 参照 zzh20188/GKI_KernelSU_SUSFS build.yml

用法:
  python3 build.py            # 全流程
  python3 build.py --step 9   # 从步骤 9 开始
  python3 build.py --only 9   # 只跑步骤 9
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.yaml"
LOG_FILE = ROOT / "build" / "build.log"

_VENV_PY = ROOT / ".venv" / "bin" / "python"
if _VENV_PY.exists() and sys.executable != str(_VENV_PY):
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])


def load_config(path: Path) -> dict:
    import yaml
    if not path.exists():
        raise SystemExit(f"缺少配置文件: {path}")
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------- 日志 (终端+文件)
class Log:
    RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
    RED, GREEN, YELLOW, CYAN = "\033[31m", "\033[32m", "\033[33m", "\033[36m"
    _f = None

    @staticmethod
    def init_file(p):
        p.parent.mkdir(parents=True, exist_ok=True)
        Log._f = open(p, "a", encoding="utf-8")

    @staticmethod
    def _emit(s):
        print(s, flush=True)
        if Log._f:
            Log._f.write(s + "\n")
            Log._f.flush()

    @staticmethod
    def step(n, t):
        Log._emit(f"\n{Log.CYAN}===== [{n}] {t} {Log.DIM}{time.strftime('%H:%M:%S')}{Log.RESET}")

    @staticmethod
    def ok(m=""):
        Log._emit(f"  {Log.GREEN}✓{Log.RESET} {m}")

    @staticmethod
    def warn(m):
        Log._emit(f"  {Log.YELLOW}!{Log.RESET} {m}")

    @staticmethod
    def err(m):
        Log._emit(f"  {Log.RED}✗{Log.RESET} {m}")

    @staticmethod
    def info(m):
        Log._emit(f"  {Log.DIM}{m}{Log.RESET}")


class BuildError(Exception):
    pass


def run(cmd, cwd=None, check=True, timeout=None, log_output=False):
    cwd = str(cwd) if cwd else None
    Log.info(f"$ {cmd}" + (f"  (cwd={cwd})" if cwd else ""))
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd,
                           capture_output=not log_output, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BuildError(f"命令超时 ({timeout}s): {cmd}")
    if r.returncode != 0 and check:
        tail = (r.stderr or r.stdout or "")[-3000:]
        raise BuildError(f"命令失败 ({r.returncode}): {cmd}\n--- 输出尾部 ---\n{tail}")
    return r


@dataclass
class Step:
    id: float
    title: str
    fn: callable


class Builder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.env = cfg.get("build", {})
        self.kernel_root = ROOT / self.env.get("kernel_dir", "build/repo")
        self.steps = []

    def register(self, s):
        self.steps.append(s)

    # ================= 0. 环境 =================
    def step00(self):
        Log.step(0, "构建环境准备")
        (ROOT / "build").mkdir(exist_ok=True)
        (ROOT / "out").mkdir(exist_ok=True)
        if not _VENV_PY.exists():
            run(f"python3 -m venv {ROOT/'.venv'}")
            run(f"{_VENV_PY} -m pip install --quiet pyyaml")
            Log.ok("venv 就绪")
        repo_bin = ROOT / "third_party" / "git-repo" / "repo"
        if not repo_bin.exists():
            (ROOT / "third_party" / "git-repo").mkdir(parents=True, exist_ok=True)
            run(f"curl -s \"https://gerrit.googlesource.com/git-repo/+/refs/tags/v2.16/repo?format=TEXT\" | base64 -d > {repo_bin}", timeout=120)
            repo_bin.chmod(0o755)
        self.REPO = str(repo_bin)
        Log.ok("环境就绪")

    # ================= 1. 工具链 =================
    def step01(self):
        Log.step(1, "准备工具链 (kernel-build-tools + mkbootimg)")
        tc = ROOT / "third_party" / "toolchain"
        tc.mkdir(parents=True, exist_ok=True)
        br = "main-kernel-build-2024"
        src = ROOT / "third_party"
        if not (tc / "kernel-build-tools" / "linux-x86").exists():
            if (src / "kernel-build-tools" / "linux-x86").exists():
                shutil.copytree(src / "kernel-build-tools", tc / "kernel-build-tools")
            else:
                for url, name in [("https://android.googlesource.com/kernel/prebuilts/build-tools", "google"),
                                  ("https://mirrors.tuna.tsinghua.edu.cn/git/AOSP/kernel/prebuilts/build-tools", "tuna")]:
                    r = run(f"git clone --depth 1 --single-branch -b {br} {url} {tc/'kernel-build-tools'}", check=False, timeout=1800)
                    if r.returncode == 0:
                        Log.ok(f"来自 {name}")
                        break
                else:
                    raise BuildError("kernel-build-tools 获取失败")
        if not (tc / "mkbootimg" / "mkbootimg.py").exists():
            if (src / "mkbootimg" / "mkbootimg.py").exists():
                shutil.copytree(src / "mkbootimg", tc / "mkbootimg")
            else:
                for url, name in [("https://android.googlesource.com/platform/system/tools/mkbootimg", "google"),
                                  ("https://mirrors.tuna.tsinghua.edu.cn/git/AOSP/platform/system/tools/mkbootimg", "tuna")]:
                    r = run(f"git clone --depth 1 --single-branch -b {br} {url} {tc/'mkbootimg'}", check=False, timeout=600)
                    if r.returncode == 0:
                        Log.ok(f"来自 {name}")
                        break
                else:
                    raise BuildError("mkbootimg 获取失败")
        Log.ok("工具链就绪")

    # ================= 2. 依赖 =================
    def step02(self):
        Log.step(2, "准备依赖仓库")
        deps = {
            "AnyKernel3": ("https://github.com/WildKernels/AnyKernel3.git", "gki-2.0"),
            "kernel_patches": ("https://github.com/WildKernels/kernel_patches.git", None),
            "Action-Build": ("https://github.com/Numbersf/Action-Build.git", None),
        }
        tp = ROOT / "third_party"
        for name, (url, branch) in deps.items():
            d = tp / name
            if d.exists() and any(d.iterdir()):
                Log.ok(f"{name} 已存在")
                continue
            if branch:
                run(f"git clone {url} -b {branch} {d}", timeout=900)
            else:
                run(f"git clone {url} --depth=1 {d}", timeout=900)
            if name == "AnyKernel3":
                shutil.rmtree(d / ".git", ignore_errors=True)
            Log.ok(f"{name} 就绪")

    # ================= 3. 签名密钥 =================
    def step03(self):
        Log.step(3, "生成签名密钥 + git 配置")
        key = ROOT / "third_party" / "toolchain" / "kernel-build-tools" / "linux-x86" / "share" / "avb" / "testkey_rsa2048.pem"
        key.parent.mkdir(parents=True, exist_ok=True)
        run(f"openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 > {key}")
        run('git config --global user.name "BuildBot"')
        run('git config --global user.email "BuildGkiKernel@gmail.com"')
        Log.ok("密钥已生成")

    # ================= 4. 源码恢复 =================
    def step04(self):
        Log.step(4, "恢复干净内核源码")
        kr = self.kernel_root
        kr.mkdir(parents=True, exist_ok=True)
        common = kr / "common"
        # ===== 基线: 6.6.77 (2025-03) / 6.6.118 (2026-01) =====
        base = self.env.get("kernel_base", "6.6.77")
        if base == "6.6.118":
            branch = "android15-6.6-2026-01"
            self.env["os_patch_level"] = "2026-01"
        else:
            branch = "deprecated/android15-6.6-2025-03"
            self.env["os_patch_level"] = "2025-03"
        if not (common / "Makefile").exists():
            # 自动拉取内核源码 (参考 GKI_KernelSU_SUSFS: repo manifest + kleaf tools)
            # repo sync 会拉取 common + tools/bazel + kernel/configs 等 (kleaf 构建必需)
            Log.info(f"repo init/sync 拉取源码: {branch} (manifest)")
            kr.mkdir(parents=True, exist_ok=True)
            m_branch = ("common-android15-6.6-2026-01"
                        if base == "6.6.118"
                        else "common-android15-6.6-2025-03")
            run(f"{self.REPO} init --depth=1 "
                f"-u https://android.googlesource.com/kernel/manifest "
                f"-b {m_branch} --repo-rev=v2.16",
                cwd=kr, timeout=600)
            if base != "6.6.118":
                # deprecated 分支: manifest 里 common revision 改 deprecated/ 前缀
                mf = kr / ".repo" / "manifests" / "default.xml"
                if mf.exists():
                    txt = mf.read_text()
                    txt = txt.replace('revision="android15-6.6-2025-03"',
                                      'revision="deprecated/android15-6.6-2025-03"')
                    mf.write_text(txt)
                    Log.info("manifest: common 已指向 deprecated/android15-6.6-2025-03")
            run(f"{self.REPO} sync -c -j$(nproc --all) --no-tags --fail-fast",
                cwd=kr, timeout=7200)
            if not (common / "Makefile").exists():
                raise BuildError(f"repo sync 后 common/Makefile 仍缺失: {common}")
            # repo sync 的 common remote 名为 aosp (manifest 定义), 与 build.py 兼容
            Log.ok(f"源码就绪: {branch} (repo sync)")
        Log.info(f"基线: {base} → {branch}")
        cur = run(f"git -C {common} branch --show-current", check=False).stdout.strip()
        if cur != branch:
            Log.info(f"切换分支 {cur or '(detached)'} → {branch}")
            # 先丢弃本地修改 (step07 改过 BUILD.bazel/abi 等), 否则 checkout 被拒
            run(f"git -C {common} checkout -q -- .", check=False)
            run(f"git -C {common} clean -qfd", check=False)
            # 注意: 不能用 "A || B && C" 链 (shell 优先级 (A||B)&&C, A 成功时 C 仍执行,
            # FETCH_HEAD 是陈旧引用会污染分支). 顺序执行 + 校验.
            r = run(f"git -C {common} checkout -q -B {branch} aosp/{branch}",
                    check=False)
            if r.returncode != 0:
                run(f"git -C {common} fetch -q aosp {branch}", check=False)
                run(f"git -C {common} checkout -q -B {branch} FETCH_HEAD",
                    check=False)
            # 校验: 分支必须指向 aosp/{branch} 的 commit (防 FETCH_HEAD 污染)
            want = run(f"git -C {common} rev-parse aosp/{branch}",
                       check=False).stdout.strip()
            got = run(f"git -C {common} rev-parse {branch}",
                      check=False).stdout.strip()
            if want and got and want != got:
                raise BuildError(f"分支 {branch} 指向错误: 期望 {want[:12]}, 实际 {got[:12]}")
        # 恢复干净 (失败必须报错, 不允许静默污染)
        run(f"git -C {common} reset --hard", check=True)
        run(f"git -C {common} checkout -- .", check=True)
        run(f"git -C {common} clean -fd", check=True)
        # 清嵌套 .git 仓库 (如 Baseband-guard, git clean 不删), 防源码污染
        for d in common.iterdir():
            if d.is_dir() and (d / ".git").exists() and d.name not in (".git",):
                shutil.rmtree(d, ignore_errors=True)
                Log.info(f"删除嵌套仓库: {d.name}")
        # 验证: 必须 0 个 tracked 修改, 否则源码污染未清干净
        n = int(run(f"git -C {common} status --short | grep -c '^ M\\|^M\\|^ D\\|^D' || true",
                    check=False).stdout.strip() or "0")
        if n > 0:
            raise BuildError(f"源码恢复不彻底: 仍有 {n} 个 tracked 修改 (污染), 手动检查:\n"
                             f"  cd {common} && git status --short")
        bk = kr / "build" / "kernel"
        if (bk / ".git").exists():
            run(f"git -C {bk} checkout -q -B main-kernel-build-2024 aosp/main-kernel-build-2024 2>/dev/null || true", check=False)
        n = run(f"git -C {common} status --short | wc -l", check=False).stdout.strip()
        Log.ok(f"common 已恢复干净 (剩余修改: {n})")

        # ===== ACK tag 自动升级 (6.6.118 → 最新 android15-6.6-2026-01_rNN) =====
        if base == "6.6.118":
            self._upgrade_ack_tag(common, branch)

        # ===== LTS 分支合并 (6.6.y LTS 更新, 可选) =====
        if base == "6.6.118" and self.env.get("lts_merge", False):
            self._merge_lts_branch(common, branch)

    # ================= 4.6 LTS 分支合并 (android15-6.6-lts) =================
    def _merge_lts_branch(self, common, branch):
        """合并 ACK android15-6.6-lts 分支 (Google 官方 LTS 更新, KMI 保证兼容)

        策略:
          1. 干净合并 (工作区无补丁时执行, 补丁在 step06b 重新应用)
          2. ABI 文件用并集 (r39 小米私有符号 + LTS 新符号), 驱动全兼容
          3. 冲突时中止构建, 人工解决 (不自动 -X 解决)
        """
        Log.step(4.6, "LTS 分支合并 (android15-6.6-lts)")
        # 1. 确认本地有 LTS 分支对象
        lts_tip = run(f"git -C {common} rev-parse aosp/android15-6.6-lts",
                      check=False).stdout.strip()
        if not lts_tip:
            Log.info("fetch LTS 分支...")
            run(f"git -C {common} fetch -q aosp android15-6.6-lts",
                check=False)
            lts_tip = run(f"git -C {common} rev-parse FETCH_HEAD",
                          check=False).stdout.strip()
        if not lts_tip:
            raise BuildError("无法获取 android15-6.6-lts 分支")
        # 2. 已合并? (当前 HEAD 包含 LTS tip 或 LTS tip == HEAD)
        cur = run(f"git -C {common} rev-parse HEAD", check=False).stdout.strip()
        if cur == lts_tip:
            Log.ok("已是最新 LTS")
            return
        merged = run(f"git -C {common} merge-base --is-ancestor {lts_tip} HEAD",
                     check=False).returncode == 0
        if merged:
            Log.ok(f"LTS 已合并进当前分支 ({lts_tip[:12]})")
            return
        # 3. 合并
        Log.info(f"合并 LTS: {lts_tip[:12]} (SUBLEVEL 118 → 142+)")
        r = run(f"git -C {common} merge --no-commit --no-ff {lts_tip}",
                check=False)
        if r.returncode != 0:
            # 冲突: 自动解决已知类别
            Log.warn("合并冲突, 自动解决...")
            self._auto_resolve_conflicts(common, lts_tip)
            abi_ok = self._merge_abi_union(common, lts_tip)
            if not abi_ok:
                run(f"git -C {common} merge --abort", check=False)
                raise BuildError("LTS 合并冲突 (ABI 处理失败), 需人工解决")
            # 重新检查剩余冲突
            left = run(f"git -C {common} diff --name-only --diff-filter=U",
                       check=False).stdout.strip()
            if left:
                run(f"git -C {common} merge --abort", check=False)
                raise BuildError(f"LTS 合并冲突: {left}, 需人工解决")
        run(f"git -C {common} commit -q --no-edit", check=False)
        Log.ok(f"LTS 合并完成: {lts_tip[:12]}")

    def _auto_resolve_conflicts(self, common, lts_tip):
        """自动解决已知类别的合并冲突

        策略 (基于 6.6.118→142 手动合并经验):
          1. 空侧冲突: 一侧为空 → 取非空侧 (LTS 新增/r39 独有)
          2. vendor_hooks.c: 并集 (双方 hook 导出都保留)
          3. 已知 LTS 重构文件: 取 LTS 版 (旧字段/旧函数已删除)
        """
        import subprocess as _sp
        left = _sp.run(["git", "diff", "--name-only", "--diff-filter=U"],
                       cwd=common, capture_output=True, text=True).stdout.splitlines()
        # LTS 重构文件 (取 theirs, 旧 API 已删)
        lts_refactor = {
            "arch/arm64/kvm/hyp/nvhe/pkvm.c",
            "drivers/dma-buf/dma-buf.c",
            "block/bio.c",
        }
        for f in left:
            f = f.strip()
            if not f or f == "Changes:":
                continue
            p = common / f
            if not p.exists():
                continue
            with open(p) as fh:
                lines = fh.readlines()
            if not any("<<<<<<<" in l for l in lines):
                continue
            out = []
            i = 0
            while i < len(lines):
                if "<<<<<<<" in lines[i]:
                    ours = []
                    i += 1
                    while i < len(lines) and "=======" not in lines[i]:
                        ours.append(lines[i]); i += 1
                    i += 1
                    theirs = []
                    while i < len(lines) and ">>>>>>>" not in lines[i]:
                        theirs.append(lines[i]); i += 1
                    i += 1
                    ours_txt = "".join(ours).strip()
                    theirs_txt = "".join(theirs).strip()
                    if f == "drivers/android/vendor_hooks.c":
                        # 并集
                        seen = set(out)
                        for l in ours + theirs:
                            if l.strip() and l not in seen:
                                out.append(l); seen.add(l)
                    elif f in lts_refactor:
                        out.extend(theirs)
                    elif not ours_txt and theirs_txt:
                        out.extend(theirs)
                    elif not theirs_txt and ours_txt:
                        out.extend(ours)
                    else:
                        # 无法判断, 保留冲突标记
                        out.append("<<<<<<< HEAD\n")
                        out.extend(ours)
                        out.append("=======\n")
                        out.extend(theirs)
                        out.append(">>>>>>> aosp/android15-6.6-lts\n")
                else:
                    out.append(lines[i]); i += 1
            with open(p, "w") as fh:
                fh.writelines(out)
            _sp.run(["git", "add", f], cwd=common, check=False)
            Log.info(f"  自动解决: {f}")

    def _merge_abi_union(self, common, lts_tip):
        """ABI 文件并集: 保留 r39 符号 + 追加 LTS 符号 (驱动兼容)"""
        import subprocess as _sp
        ok = True
        for abi in ["android/abi_gki_aarch64.stg",
                    "android/abi_gki_aarch64_xiaomi",
                    "android/abi_gki_aarch64_oplus"]:
            p = common / abi
            if not p.exists():
                continue
            # 冲突阶段: ours (HEAD) = r39, theirs = LTS
            ours = p.read_text().splitlines()
            theirs = _sp.run(["git", "show", f"{lts_tip}:{abi}"],
                             cwd=common, capture_output=True, text=True).stdout.splitlines()
            # 并集: 保留 r39 所有行 + LTS 独有的行
            ours_set = set(ours)
            merged = list(ours)
            for line in theirs:
                if line not in ours_set:
                    merged.append(line)
            p.write_text("\n".join(merged) + "\n")
            _sp.run(["git", "add", abi], cwd=common, check=False)
            Log.info(f"ABI 并集: {abi} ({len(ours)} + {len(theirs) - len(ours_set & set(theirs))} 新增)")
        return ok

    def _upgrade_ack_tag(self, common, branch):
        """检测并合并最新 ACK release tag (如 r39 → r40)"""
        import re as _re
        Log.step(4.5, f"ACK tag 检查: {branch}")
        # 1. 查询远程最新 tag (排序取最大 rNN)
        out = run(f"git ls-remote --tags aosp 'android15-6.6-2026-01_r*'",
                  check=False).stdout
        tags = []
        for line in out.splitlines():
            m = _re.search(r"android15-6.6-2026-01_r(\d+)\^?\{\}?$", line.strip())
            if m:
                tags.append(int(m.group(1)))
        if not tags:
            Log.warn("远程无 ACK tag (网络/镜像问题), 保持当前版本")
            return
        latest = max(tags)
        want_tag = f"android15-6.6-2026-01_r{latest}"
        # 2. 本地是否已有该 tag
        have = run(f"git -C {common} tag -l {want_tag}", check=False).stdout.strip()
        if not have:
            Log.info(f"发现新 ACK tag {want_tag}, fetch 中...")
            r = run(f"git -C {common} fetch -q aosp tag {want_tag}", check=False)
            if r.returncode != 0:
                Log.warn(f"fetch {want_tag} 失败 (网络), 保持当前版本")
                return
        # 3. 比较: 当前分支 tip 是否已是该 tag
        tip = run(f"git -C {common} rev-parse HEAD", check=False).stdout.strip()
        tagc = run(f"git -C {common} rev-parse {want_tag}^{{}}", check=False).stdout.strip()
        if tip == tagc:
            Log.ok(f"已是最新 ACK tag: {want_tag} ({tip[:12]})")
            return
        # 4. 不是最新 → 切换到最新 tag (基于当前分支的提交点)
        Log.info(f"ACK tag 升级: 当前 {tip[:12]} → {want_tag} ({tagc[:12]})")
        run(f"git -C {common} checkout -q -- .", check=False)
        run(f"git -C {common} clean -qfd", check=False)
        r = run(f"git -C {common} checkout -q -B {branch} {want_tag}^{{}}",
                check=False)
        if r.returncode != 0:
            raise BuildError(f"切换到 {want_tag} 失败")
        Log.ok(f"已切换到 ACK tag: {want_tag}")

    # ================= 5. 子版本 =================
    def step05(self):
        Log.step(5, "提取实际子版本号")
        mk = self.kernel_root / "common" / "Makefile"
        sub = None
        if mk.exists():
            for line in mk.read_text().splitlines():
                if line.startswith("SUBLEVEL = "):
                    sub = line.split("=")[1].strip()
                    break
        self.ACTUAL_SUBLEVEL = sub or self.env.get("sub_level", "77")
        Log.ok(f"SUBLEVEL = {self.ACTUAL_SUBLEVEL}")

    # ================= 5.5 WiFi/蓝牙 =================
    def step05b(self):
        if self.env.get("kernel_version") != "6.6":
            Log.warn("非 6.6 基线, 跳过")
            return
        Log.step(5.5, "修复 6.6 WiFi/蓝牙兼容性 (三星 min_kdp + 小米)")
        common = self.kernel_root / "common"
        kp = ROOT / "third_party" / "kernel_patches"

        def ensure(file, line):
            if not file.exists():
                raise BuildError(f"文件不存在: {file}")
            if line not in file.read_text():
                with open(file, "a") as f:
                    f.write(line + "\n")

        galaxy = common / "android" / "abi_gki_aarch64_galaxy"
        xiaomi = common / "android" / "abi_gki_aarch64_xiaomi"
        if not galaxy.exists() or not xiaomi.exists():
            Log.warn("厂商 ABI 符号文件不存在, 跳过")
            return
        ensure(galaxy, "kdp_set_cred_non_rcu")
        ensure(galaxy, "kdp_usecount_dec_and_test")
        ensure(galaxy, "kdp_usecount_inc")
        patch = kp / "samsung" / "min_kdp" / "add-min_kdp-symbols.patch"
        src = kp / "samsung" / "min_kdp" / "min_kdp.c"
        if patch.exists():
            r = run(f"patch -p1 --dry-run < {patch}", cwd=common, check=False)
            if r.returncode == 0:
                run(f"patch -p1 --no-backup-if-mismatch < {patch}", cwd=common)
            else:
                Log.warn("min_kdp patch 已应用或上下文不匹配, 跳过")
        if src.exists():
            shutil.copy2(src, common / "drivers" / "min_kdp.c")
            ensure(common / "drivers" / "Makefile", "obj-y += min_kdp.o")
        ensure(xiaomi, "device_find_any_child")
        Log.ok("WiFi/蓝牙兼容符号已注入")

    # ================= 5.6 KSU 注入 (ReSukiSU, 本地优先按需更新, 参照 zzh) =================
    def step05c(self):
        ksu_cfg = self.cfg.get("ksu", {})
        if not ksu_cfg.get("enabled", True):
            Log.warn("KSU 关闭, 跳过")
            return
        Log.step(5.6, "添加 KernelSU (ReSukiSU)")
        kr = self.kernel_root
        ksu_dir = kr / "KernelSU"
        self._ensure_ksu_source(ksu_dir)
        ksu_ver = run(f"git -C {ksu_dir} rev-list --count HEAD", check=False).stdout.strip()
        Log.ok(f"KernelSU 就绪: {ksu_ver} 提交 → KSU_VERSION={30000 + int(ksu_ver or 0) + 800}")
        # setup (symlink + Makefile + Kconfig)
        setup = ROOT / "third_party" / "ksu" / "setup-local.sh"
        run(f"bash {setup}", cwd=kr)
        Log.ok("KernelSU 注入完成")

    def _ensure_ksu_source(self, ksu_dir):
        """KSU 源码: 本地已有则按需更新, 没有则拉取 (自包含, 防弱网)"""
        KSU_UPSTREAM = "https://github.com/pengzenzen-creator/ReSukiSU-Ultra"
        KSU_BRANCH = "main"
        LOCAL_CACHES = [
            "/home/tees/T-677/repo/KernelSU",
            "/home/tees/kernel-build/XiaoMi_8Elite/KernelSU",
            "/home/tees/.qemu-test/KernelSU",
        ]

        # 1) 本地已有: 先更新到最新 (弱网/离线时保留旧版继续)
        if (ksu_dir / ".git").exists():
            Log.info("KernelSU 已存在, 尝试更新到最新...")
            # 兜底克隆可能把 origin 指向本地缓存路径 (2026-08-14 教训),
            # 校验并纠正回上游, 否则 fetch 只从缓存拉, 永远"已是最新"假象
            cur_origin = run(f"git -C {ksu_dir} remote get-url origin",
                             check=False).stdout.strip()
            if cur_origin != KSU_UPSTREAM:
                Log.warn(f"origin 异常 ({cur_origin}), 改回 {KSU_UPSTREAM}")
                run(f"git -C {ksu_dir} remote set-url origin {KSU_UPSTREAM}",
                    check=True)
            r = run(f"git -C {ksu_dir} fetch origin {KSU_BRANCH} --tags",
                    timeout=600, check=False)
            if r.returncode == 0:
                run(f"git -C {ksu_dir} checkout -q -B {KSU_BRANCH} "
                    f"origin/{KSU_BRANCH}", check=False)
                Log.ok("KernelSU 已更新到最新")
            else:
                Log.warn("更新失败 (网络?), 使用本地已有 KernelSU")
            return

        # 2) 本地没有: 网络克隆
        Log.info("KernelSU 不存在, 克隆上游...")
        r = run(f"git clone {KSU_UPSTREAM} {ksu_dir}", timeout=600,
                check=False)
        if r.returncode == 0:
            return

        # 3) 网络失败: 本地缓存兜底 (--local 不走网络)
        Log.warn("网络克隆失败, 尝试本地缓存...")
        for cache in LOCAL_CACHES:
            if Path(cache).exists() and (Path(cache) / ".git").exists():
                run(f"git clone --local {cache} {ksu_dir}", timeout=600,
                    check=True)
                # clone --local 会把 origin 指向缓存路径 → 强制改回上游,
                # 否则下次构建 fetch 只从本地缓存拉, 永远"已是最新"假象
                # (2026-08-14 教训: origin 指向 T-677 缓存, GitHub 上游从未拉过)
                run(f"git -C {ksu_dir} remote set-url origin {KSU_UPSTREAM}",
                    check=True)
                Log.ok(f"本地缓存克隆: {cache} (origin 已改回 {KSU_UPSTREAM})")
                return
        raise BuildError("KSU 网络克隆失败且无本地缓存")

    # ================= 5.7 KSU seccomp PF_EXITING 防死机 (6.6.118+) =================
    def step05d(self):
        Log.step(5.7, "KSU seccomp PF_EXITING 兼容修复 (6.6.118+ 防死机)")
        base = self.env.get("kernel_base", "6.6.77")
        if base != "6.6.118":
            Log.warn(f"基线 {base} 不需要, 跳过")
            return
        app_profile = self.kernel_root / "KernelSU" / "kernel" / "policy" / "app_profile.c"
        if app_profile.exists() and "KERNEL_VERSION(6, 11, 0)" in app_profile.read_text():
            txt = app_profile.read_text().replace(
                "KERNEL_VERSION(6, 11, 0)", "KERNEL_VERSION(6, 6, 118)")
            app_profile.write_text(txt)
            Log.ok("PF_EXITING 兼容修复已注入 (6.6.118+)")
            # commit 修复 (消除 KSU 版本号 -dirty 标记)
            ksu_git = self.kernel_root / "KernelSU"
            r = run(f"git -C {ksu_git} diff --quiet", check=False)
            if r.returncode != 0:
                run(f"git -C {ksu_git} add kernel/policy/app_profile.c", check=False)
                run(f"git -C {ksu_git} commit -qm 'tesla: seccomp PF_EXITING fix (6.6.118+)'", check=False)
                Log.ok("KSU 修改已 commit (版本号无 -dirty)")
        else:
            Log.warn("app_profile.c 未找到或已修复, 跳过")

    # ================= 5.8 SUSFS 补丁 (参照 zzh) =================
    def step05e(self):
        susfs_cfg = self.cfg.get("susfs", {})
        if not susfs_cfg.get("enabled", True):
            Log.warn("SUSFS 关闭, 跳过")
            return
        Log.step(5.8, "应用 SUSFS 补丁")
        common = self.kernel_root / "common"
        susfs = ROOT / "third_party" / "susfs4ksu"
        av, kv = self.env.get("android_version"), self.env.get("kernel_version")
        # SUSFS 自动跟随上游 (参考 GKI_KernelSU_SUSFS: gitlab simonpunk 最新)
        susfs_branch = f"gki-{av}-{kv}"
        if (susfs / ".git").exists():
            run(f"git -C {susfs} fetch origin {susfs_branch}", check=False)
            run(f"git -C {susfs} checkout -q -B {susfs_branch} FETCH_HEAD", check=False)
            Log.ok(f"SUSFS 已更新至上游最新 ({susfs_branch})")
        else:
            # 仓库提交的副本 (非 git) → 移除后克隆上游最新
            import shutil
            if susfs.exists():
                shutil.rmtree(susfs)
            run(f"git clone https://gitlab.com/simonpunk/susfs4ksu.git "
                f"-b {susfs_branch} {susfs}", timeout=900)
            Log.ok(f"SUSFS 已克隆上游最新 ({susfs_branch})")
        patch = susfs / "kernel_patches" / f"50_add_susfs_in_gki-{av}-{kv}.patch"
        if not patch.exists():
            raise BuildError(f"SUSFS 补丁缺失: {patch}")
        shutil.copy2(patch, common / patch.name)
        # fs/ 和 include/linux/ 复制
        for sub in ("fs", "include/linux"):
            src = susfs / "kernel_patches" / sub
            dst = common / sub
            if src.exists():
                for f in src.iterdir():
                    if f.is_file():
                        shutil.copy2(f, dst / f.name)
        # ReSukiSU 内置 SUSFS, 无需 KernelSU 内补丁
        # 临时调整上下文 (zzh 同款): 6.6.77 官方 base.c 无 dma-buf.h, SUSFS 补丁期望它存在
        base_c = common / "fs" / "proc" / "base.c"
        if base_c.exists() and "#include <linux/dma-buf.h>" not in base_c.read_text():
            txt = base_c.read_text()
            txt = txt.replace("#include <linux/cpufreq_times.h>",
                              "#include <linux/cpufreq_times.h>\n#include <linux/dma-buf.h>")
            base_c.write_text(txt)
            Log.info("base.c: 已补 dma-buf.h (SUSFS 上下文)")
        # 应用主补丁 (zzh 用 patch -p1; 干净源码下应成功)
        r = run(f"patch -p1 --batch < {patch.name}", cwd=common, check=False)
        if r.returncode != 0:
            raise BuildError(f"SUSFS 补丁应用失败 (源码需干净, 先跑 step4):\n{r.stderr[-1000:]}")
        Log.ok("SUSFS 主补丁已应用")
        Log.ok("SUSFS 补丁应用完成 (defconfig 在 step06b 注入)")

    # ================= 6. defconfig =================
    def step06(self):
        Log.step(6, "配置内核选项 (KSU/TMPFS) + 备份基准 defconfig")
        defconfig = self.kernel_root / "common" / "arch" / "arm64" / "configs" / "gki_defconfig"
        self.defconfig = defconfig
        shutil.copy2(defconfig, f"{defconfig}.orig")
        # KSU + TMPFS (zzh 配置内核选项; KSU 关闭时 (FolkPatch/纯净版) 不注入 CONFIG_KSU)
        lines = ["CONFIG_TMPFS_XATTR=y", "CONFIG_TMPFS_POSIX_ACL=y"]
        if self.cfg.get("ksu", {}).get("enabled", True):
            lines.append("CONFIG_KSU=y")
        txt = defconfig.read_text()
        for c in lines:
            if c not in txt:
                txt += c + "\n"
        defconfig.write_text(txt)
        bcg = self.kernel_root / "common" / "build.config.gki"
        if bcg.exists():
            bcg.write_text(bcg.read_text().replace("check_defconfig", ""))
        # ===== 固定 fragment (替代 .orig diff 机制, LTS 合并后更可靠) =====
        # 注: 直接写 tesla.fragment, 构建时 --defconfig_fragment 自动启用;
        #     不碰 defconfig 顺序, 避免 savedefconfig 校验失败
        frag_lines = ["CONFIG_TMPFS_XATTR=y", "CONFIG_TMPFS_POSIX_ACL=y",
                      "CONFIG_KSU=y",
                      "CONFIG_IP_SET=y", "CONFIG_IP_SET_BITMAP_IP=y",
                      "CONFIG_IP_SET_BITMAP_IPMAC=y", "CONFIG_IP_SET_BITMAP_PORT=y",
                      "CONFIG_IP_SET_HASH_IP=y", "CONFIG_IP_SET_HASH_IPMARK=y",
                      "CONFIG_IP_SET_HASH_IPPORT=y", "CONFIG_IP_SET_HASH_IPPORTIP=y",
                      "CONFIG_IP_SET_HASH_IPPORTNET=y", "CONFIG_IP_SET_HASH_MAC=y",
                      "CONFIG_IP_SET_HASH_NET=y", "CONFIG_IP_SET_HASH_NETIFACE=y",
                      "CONFIG_IP_SET_HASH_NETNET=y", "CONFIG_IP_SET_HASH_NETPORT=y",
                      "CONFIG_IP_SET_HASH_NETPORTNET=y", "CONFIG_IP_SET_LIST_SET=y",
                      "CONFIG_NETFILTER_XT_SET=y",
                      "CONFIG_MQ_IOSCHED_ADIOS=y", "CONFIG_MQ_IOSCHED_DEFAULT_ADIOS=y",
                      "CONFIG_MQ_IOSCHED_SSG=y",
                      ]
        frag = self.kernel_root / "common" / "arch" / "arm64" / "configs" / "tesla.fragment"
        frag.write_text("\n".join(frag_lines) + "\n")
        Log.ok(f"固定 fragment: {len(frag_lines)} 项 (含 rfkill/adios/ipset/KSU)")
        Log.ok("defconfig 已配置 + 基准已备份")

    # ================= 6.5 应用补丁 (config.yaml features 开关) =================
    def step06b(self):
        Log.step(6.5, "应用补丁 + defconfig 联动")
        import patches as pm
        features = self.cfg.get("features", {})
        on_list = [k for k, v in features.items() if v]
        if on_list:
            Log.info(f"启用补丁: {on_list}")
            results = pm.apply_all(self.cfg, self.kernel_root,
                                   sublevel=getattr(self, "ACTUAL_SUBLEVEL", "77"),
                                   kernel_base=self.env.get("kernel_base", "6.6.77"))
            for feat, msg in results:
                Log.ok(f"{feat}: {msg}")
        else:
            Log.warn("无功能补丁开启")
        # 联动 defconfig
        defconfig = getattr(self, "defconfig", self.kernel_root / "common" / "arch" / "arm64" / "configs" / "gki_defconfig")

        def add_cfg(lines):
            txt = defconfig.read_text()
            for c in lines:
                if c not in txt:
                    txt += c + "\n"
            defconfig.write_text(txt)

        if features.get("uksm"):
            add_cfg(["CONFIG_KSM=y", "CONFIG_UKSM=y", "CONFIG_UKSM_CPU_GOVERNOR=1"])
            Log.ok("UKSM defconfig: KSM/UKSM/CPU_GOVERNOR=1")
        if features.get("adios"):
            add_cfg(["CONFIG_MQ_IOSCHED_ADIOS=y", "CONFIG_MQ_IOSCHED_DEFAULT_ADIOS=y"])
            Log.ok("ADIOS defconfig: 默认 IO 调度器 = ADIOS")
        if features.get("ssg"):
            # SSG IO 调度器 (Samsung) — 不开 SSG_CGROUP (6.6 无 cpd_init_fn, ratio=0 降速)
            add_cfg(["CONFIG_MQ_IOSCHED_SSG=y"])
            Log.ok("SSG defconfig: SSG IO 调度器 (可选切换, 不设默认)")
        if features.get("bbr3"):
            # 全开拥塞控制算法 (T-677 同款), 含 TCP_CONG_BBR3 供 DEFAULT_BBR3 选择
            add_cfg([f"CONFIG_TCP_CONG_{c}=y" for c in
                     ("BIC", "CUBIC", "WESTWOOD", "HTCP", "HSTCP", "HYBLA", "VEGAS", "NV",
                      "SCALABLE", "LP", "VENO", "YEAH", "ILLINOIS", "DCTCP", "CDG", "BBR", "BBR3")])
            add_cfg(["CONFIG_DEFAULT_BBR3=y"])
            Log.ok("BBRv3 defconfig: 全开 17 算法 + 默认 bbr3")
        if features.get("zram_lz4kd"):
            add_cfg(["CONFIG_ZSMALLOC=y"])
            comp = self.cfg.get("zram", {}).get("def_comp", "lz4kd")
            import subprocess
            subprocess.run(f"sed -i 's/CONFIG_ZRAM=m/CONFIG_ZRAM=y/g' {defconfig}", shell=True)
            subprocess.run(f"sed -i '/^CONFIG_ZRAM_DEF_COMP_/d' {defconfig}", shell=True)
            add_cfg([f"CONFIG_ZRAM_DEF_COMP_{comp.upper()}=y"])
            # ZRAM=y 内建 → 从模块列表移除 zram.ko/zsmalloc.ko (否则与内建冲突)
            mbzl = self.kernel_root / "common" / "modules.bzl"
            if mbzl.exists():
                txt = mbzl.read_text()
                txt = txt.replace('"drivers/block/zram/zram.ko",\n', "")
                txt = txt.replace('"mm/zsmalloc.ko",\n', "")
                mbzl.write_text(txt)
            # zram.config 追加 (压缩算法配置)
            zcfg = ROOT / "third_party" / "zram-stack" / "zram.config"
            if zcfg.exists():
                for line in zcfg.read_text().splitlines():
                    if line.strip() and not line.strip().startswith("#"):
                        add_cfg([line.strip()])
            Log.ok(f"zram defconfig: ZSMALLOC/ZRAM/{comp} + modules.bzl 清理")
        # SUSFS defconfig (备份后追加, 进 fragment)
        if self.cfg.get("susfs", {}).get("enabled", True):
            add_cfg([
                "CONFIG_KSU_SUSFS=y", "CONFIG_KSU_SUSFS_SUS_PATH=y",
                "CONFIG_KSU_SUSFS_SUS_MOUNT=y", "CONFIG_KSU_SUSFS_SUS_KSTAT=y",
                "CONFIG_KSU_SUSFS_SPOOF_UNAME=y", "CONFIG_KSU_SUSFS_ENABLE_LOG=y",
                "CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y",
                "CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y",
                "CONFIG_KSU_SUSFS_OPEN_REDIRECT=y", "CONFIG_KSU_SUSFS_SUS_MAP=y",
            ])
            Log.ok("SUSFS defconfig 已注入")

    # ================= 7. KMI + 版本 =================
    def step07(self):
        Log.step(7, "KMI 严格模式关闭 + setlocalversion")
        if not hasattr(self, "ACTUAL_SUBLEVEL"):
            self.ACTUAL_SUBLEVEL = self.env.get("sub_level", "77")
        common = self.kernel_root / "common"
        # ===== 固件搜索路径补丁 (修复流量: 官方固件在 vendor 分区, cmdline 只指向 /odm/firmware/o8 空目录) =====
        # 追加 /vendor/firmware_mnt/image + /vendor/modem_firmware/image 到 fw_path[]
        # (2026-08-16: 142 内核 fallback 不完整 → modem/ipa 固件缺失 → 数据通路断)
        fw_main = common / "drivers" / "base" / "firmware_loader" / "main.c"
        if fw_main.exists():
            txt = fw_main.read_text()
            if '"/vendor/firmware_mnt/image"' not in txt:
                old = '\t"/lib/firmware/updates/" UTS_RELEASE,'
                new = '\t"/vendor/firmware_mnt/image",\n\t"/vendor/modem_firmware/image",\n' + old
                if old in txt:
                    fw_main.write_text(txt.replace(old, new, 1))
                    Log.ok("固件搜索路径: +/vendor/firmware_mnt/image +/vendor/modem_firmware/image")
                else:
                    Log.warn("fw_path[] 未找到插入点 (源码可能已变)")
            else:
                Log.ok("固件搜索路径: 已包含 vendor 目录 (幂等)")
        # ===== protected modules 禁用 (LTS 合并后新机制, step04 重置会还原) =====
        # LTS 用 protected_modules_list (modules.bzl 生成) 拦截 vendor 模块导出
        # (rfkill/arc4 → wifi/蓝牙加载失败). 与 r39/官方一致: 不保护任何模块.
        mbzl = common / "modules.bzl"
        if mbzl.exists():
            txt = mbzl.read_text()
            if "def get_gki_protected_modules_list" in txt and "return []" not in txt.split("def get_gki_protected_modules_list")[1][:200]:
                old = '''def get_gki_protected_modules_list(arch = None):
    all_gki_modules = get_gki_modules_list(arch) + get_kunit_modules_list(arch)
    unprotected_modules = _COMMON_UNPROTECTED_MODULES_LIST
    protected_modules = [mod for mod in all_gki_modules if mod not in unprotected_modules]
    return protected_modules'''
                new = '''def get_gki_protected_modules_list(arch = None):
    return []'''
                if old in txt:
                    mbzl.write_text(txt.replace(old, new))
                    Log.ok("protected modules 已禁用 (返回空)")
                else:
                    Log.warn("modules.bzl protected 实现变化, 需人工确认")
            else:
                Log.ok("protected modules 已禁用 (幂等)")
        # ===== rfkill 保持模块 (版本伪装后官方 rfkill.ko 正常加载, 无需内建) =====
        # 注: 若未来改回内建 (CONFIG_RFKILL=y), 需同步从 modules.bzl 移除 rfkill.ko
        bb = common / "BUILD.bazel"
        if bb.exists():
            txt = bb.read_text()
            txt = txt.replace('        "protected_exports_list": "android/abi_gki_protected_exports_aarch64",\n', "")
            txt = txt.replace('        "kmi_symbol_list_strict_mode": True,\n', "")
            bb.write_text(txt)
        for p in (common / "android").glob("abi_gki_protected_exports_*"):
            p.unlink()
        stamp = self.kernel_root / "build" / "kernel" / "kleaf" / "impl" / "stamp.bzl"
        if stamp.exists():
            stamp.write_text(stamp.read_text().replace("-maybe-dirty", ""))
        # ===== tesla_vm_opt 固化 (按内存调优, 内核态) =====
        # 1) compaction.c/oom_kill.c: 去 static + EXPORT_SYMBOL (LTO 下保留符号)
        for fname, old, new in [
            ("mm/compaction.c",
             "static int sysctl_extfrag_threshold = 500;",
             "int sysctl_extfrag_threshold = 500;"),
            ("mm/compaction.c",
             "static int sysctl_compact_unevictable_allowed __read_mostly = CONFIG_COMPACT_UNEVICTABLE_DEFAULT;",
             "int sysctl_compact_unevictable_allowed __read_mostly = CONFIG_COMPACT_UNEVICTABLE_DEFAULT;"),
            ("mm/oom_kill.c",
             "static int sysctl_oom_dump_tasks = 1;",
             "int sysctl_oom_dump_tasks = 1;"),
        ]:
            fp = common / fname
            if not fp.exists():
                continue
            txt = fp.read_text()
            # a) 去 static (精确行匹配, 避免子串误判)
            old_line = "\n" + old + "\n"
            new_line = "\n" + new + "\n"
            if old_line in "\n" + txt + "\n":
                txt = txt.replace(old, new)
                Log.ok(f"tesla_vm_opt: {fname} 去 static")
            # b) 补 export.h
            if "#include <linux/export.h>" not in txt:
                lines = txt.splitlines()
                for i, l in enumerate(lines):
                    if l.startswith("#include"):
                        lines.insert(i+1, "#include <linux/export.h>")
                        break
                txt = "\n".join(lines) + "\n"
                Log.ok(f"tesla_vm_opt: {fname} 补 export.h")
            # c) EXPORT_SYMBOL (LTO 必须, 否则 undefined symbol)
            sym = new.split("int ")[1].split(" ")[0]
            if sym in txt and f"EXPORT_SYMBOL({sym})" not in txt:
                lines = txt.splitlines()
                for i, l in enumerate(lines):
                    if l.strip().startswith(f"int {sym}") or l.strip().startswith(f"int {sym} "):
                        lines.insert(i+1, f"EXPORT_SYMBOL({sym});")
                        break
                txt = "\n".join(lines) + "\n"
                Log.ok(f"tesla_vm_opt: {sym} EXPORT_SYMBOL 已加")
            fp.write_text(txt)
        # 2) 拷贝 tesla_vm_opt.c + 挂载 mm/Makefile
        src = ROOT / "third_party" / "vm-opt" / "tesla_vm_opt.c"
        dst = common / "mm" / "tesla_vm_opt.c"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            Log.ok("tesla_vm_opt.c 已拷贝")
        mmk = common / "mm" / "Makefile"
        if mmk.exists():
            txt = mmk.read_text()
            if "tesla_vm_opt.o" not in txt:
                mmk.write_text(txt.rstrip("\n") + "\nobj-y += tesla_vm_opt.o\n")
                Log.ok("mm/Makefile: tesla_vm_opt.o 已挂载")
        # ===== adios 默认调度器固化 (elevator.c) =====
        # 1) elevator_get_default: CONFIG_MQ_IOSCHED_DEFAULT_ADIOS → 默认 adios
        # 2) elevator_switch: 拦截已禁用 (2026-08-18, 管理器 IO 调度器切换功能需要自由切换)
        ev = common / "block" / "elevator.c"
        if ev.exists():
            txt = ev.read_text()
            # get_default 改 adios
            if "CONFIG_MQ_IOSCHED_DEFAULT_ADIOS" not in txt.split("elevator_get_default")[1][:300]:
                old = '	return elevator_find_get(q, "mq-deadline");'
                new = ('#ifdef CONFIG_MQ_IOSCHED_DEFAULT_ADIOS\n'
                       '	return elevator_find_get(q, "adios");\n'
                       '#else\n'
                       '	return elevator_find_get(q, "mq-deadline");\n'
                       '#endif')
                if old in txt:
                    txt = txt.replace(old, new, 1)
                    Log.ok("elevator.c: 默认调度器 → adios")
            ev.write_text(txt)
        # ===== MGLRU 强制开启固化 =====
        # init.rc 会写 /sys/kernel/mm/lru_gen/enabled 0 (关闭), 这里拦截
        vs = common / "mm" / "vmscan.c"
        if vs.exists():
            txt = vs.read_text()
            if "Tesla: MGLRU" not in txt:
                old_fn = "void lru_gen_change_state(bool enabled)\n{\n\tstatic DEFINE_MUTEX(state_mutex);"
                if old_fn in txt:
                    add = ("void lru_gen_change_state(bool enabled)\n"
                           "{\n"
                           "\tstatic DEFINE_MUTEX(state_mutex);\n"
                           "\n"
                           "#ifdef CONFIG_LRU_GEN_ENABLED\n"
                           "\t/* Tesla: MGLRU 编译时启用, 拒绝运行时关闭 (init.rc 会写 0) */\n"
                           "\tif (!enabled) {\n"
                           "\t\tpr_info(\"lru_gen: 忽略关闭请求 (CONFIG_LRU_GEN_ENABLED 强制开启)\\n\");\n"
                           "\t\treturn;\n"
                           "\t}\n"
                           "#endif\n")
                    txt = txt.replace(old_fn, add, 1)
                    vs.write_text(txt)
                    Log.ok("vmscan.c: MGLRU 强制开启 (拒绝关闭)")
                else:
                    Log.warn("vmscan.c: lru_gen_change_state 签名变化, 需人工")
            else:
                Log.ok("vmscan.c: MGLRU 保护已存在 (幂等)")
            # 强制 sysfs 写入值 = 3 (CORE | MM_WALK 全开), 防任何写 1/2 的行为
            if "Tesla: MGLRU FORCE3" not in txt:
                old_store = '\telse if (kstrtouint(buf, 0, &caps))\n\t\treturn -EINVAL;\n'
                if old_store in txt:
                    add3 = ('\telse if (kstrtouint(buf, 0, &caps))\n'
                            '\t\treturn -EINVAL;\n'
                            '\n'
                            '#ifdef CONFIG_LRU_GEN_ENABLED\n'
                            '\t/* Tesla: MGLRU FORCE3 强制全开 (CORE | MM_WALK), 拒绝任何运行时修改 */\n'
                            '\tcaps = 3;\n'
                            '#endif\n')
                    txt = txt.replace(old_store, add3, 1)
                    vs.write_text(txt)
                    Log.ok("vmscan.c: MGLRU enabled_store 强制 3 (CORE|MM_WALK)")
                else:
                    Log.warn("vmscan.c: enabled_store 签名变化, 需人工")
            else:
                Log.ok("vmscan.c: MGLRU FORCE3 已存在 (幂等)")
                Log.ok("vmscan.c: MGLRU FORCE3 已存在 (幂等)")
        # ===== nr_requests 默认 256 固化 (BLKDEV_DEFAULT_RQ 128→256) =====
        bmh = common / "include" / "linux" / "blk-mq.h"
        if bmh.exists():
            txt = bmh.read_text()
            if "#define BLKDEV_DEFAULT_RQ\t128" in txt:
                bmh.write_text(txt.replace("#define BLKDEV_DEFAULT_RQ\t128",
                                           "#define BLKDEV_DEFAULT_RQ\t256"))
                Log.ok("blk-mq.h: BLKDEV_DEFAULT_RQ 128→256 (nr_requests)")
        # ===== ReSukiSU Ultra 管理器签名固化 =====
        # (消除"非官方管理器"提示: 自定义 keystore 签名加进 KSU 官方列表)
        msh = common / "KernelSU" / "kernel" / "manager" / "manager_sign.h"
        if msh.exists():
            txt = msh.read_text()
            if "RESUKISU_ULTRA" not in txt:
                add = """\n// ReSukiSU-Ultra (pengzenzen-creator)\n#define EXPECTED_SIZE_RESUKISU_ULTRA 0x376\n#define EXPECTED_HASH_RESUKISU_ULTRA "18d8d2e4ca7bfbbf967336b947a09d4413f7a24abd4b9ca1654494d2741cbe64"\n"""
                anchor2 = "// KOWX712/KernelSU"
                if anchor2 in txt:
                    msh.write_text(txt.replace(anchor2, add + anchor2))
                    Log.ok("manager_sign.h: ReSukiSU Ultra 签名已加")
        asc = common / "KernelSU" / "kernel" / "manager" / "apk_sign.c"
        if asc.exists():
            txt = asc.read_text()
            old = '    { EXPECTED_SIZE_RESUKISU, EXPECTED_HASH_RESUKISU }, /* ReSukiSU/ReSukiSU */'
            if "RESUKISU_ULTRA" not in txt and old in txt:
                asc.write_text(txt.replace(old, old + '\n    { EXPECTED_SIZE_RESUKISU_ULTRA, EXPECTED_HASH_RESUKISU_ULTRA }, /* ReSukiSU-Ultra */'))
                Log.ok("apk_sign.c: ReSukiSU Ultra 签名已引用")
        sl = common / "scripts" / "setlocalversion"
        if sl.exists():
            sl.write_text(sl.read_text().replace("-dirty", ""))
        gh = subprocess.run(["git", "rev-parse", "--verify", "HEAD", "--short=13"],
                            cwd=common, capture_output=True, text=True)
        # 版本串策略: 保持默认 (不伪装, 2026-08-17)
        # 内核显示真实版本 (KERNELVERSION + 默认 scm 版本), 不做官方串固定
        if sl.exists():
            import re
            txt = sl.read_text()
            pattern = r'(.*)echo "\$\{KERNELVERSION\}\$\{file_localversion\}\$\{config_localversion\}\$\{LOCALVERSION\}\$\{scm_version\}"'
            txt = re.sub(pattern,
                         f'\\1echo "${{KERNELVERSION}}${{config_localversion}}"',
                         txt, flags=re.S)
            sl.write_text(txt)
            Log.ok("版本格式: 默认 (不伪装)")

    # ================= 8. 时间戳 =================
    def step08(self):
        Log.step(8, "设置构建时间 (mkcompile_h)")
        bt = self.env.get("build_time") or "N"
        if bt and bt not in ("N", "n"):
            datestr = bt
        else:
            datestr = datetime.datetime.now(datetime.timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
            os.environ["KBUILD_BUILD_TIMESTAMP"] = datestr
        f = self.kernel_root / "common" / "scripts" / "mkcompile_h"
        if f.exists():
            import re
            txt = f.read_text()
            # 幂等: 先移除旧注入对 (若有), 再插入新的 (原 if UTS_VERSION= 分支为死代码)
            txt = re.sub(r'#undef UTS_VERSION\s*\n#define UTS_VERSION ".*?"\s*\n', '', txt)
            txt = txt.replace("cat <<EOF",
                              f'cat <<EOF\n#undef UTS_VERSION\n#define UTS_VERSION "#1 SMP PREEMPT {datestr}"')
            f.write_text(txt)
          setup_env = self.kernel_root / "build" / "kernel" / "kleaf" / "_setup_env.sh"
       if setup_env.exists():
           txt = setup_env.read_text()
           txt = "\n".join(l for l in txt.splitlines()
                           if not l.startswith("export KBUILD_BUILD_TIMESTAMP="))
           txt += '\nexport KBUILD_BUILD_TIMESTAMP="${KBUILD_BUILD_TIMESTAMP:-$(date -d @${SOURCE_DATE_EPOCH})}"\n'
           setup_env.write_text(txt)
        Log.ok(f"构建时间: {datestr}")

    # ================= 9. 编译 =================
    def step09(self):
        Log.step(9, "编译内核 (bazel)")
        kr = self.kernel_root
        bca = kr / "common" / "build.config.gki.aarch64"
        if bca.exists():
            txt = bca.read_text()
            txt = txt.replace("BUILD_SYSTEM_DLKM=1", "BUILD_SYSTEM_DLKM=0")
            txt = "\n".join(l for l in txt.splitlines() if "MODULES_ORDER=android/gki_aarch64_modules" not in l
                            and "KMI_SYMBOL_LIST_STRICT_MODE" not in l)
            bca.write_text(txt)
            Log.ok("BUILD_SYSTEM_DLKM=0 已应用")

        frag = kr / "common" / "arch" / "arm64" / "configs" / "tesla.fragment"
        defconfig = getattr(self, "defconfig", kr / "common" / "arch" / "arm64" / "configs" / "gki_defconfig")
        orig = f"{defconfig}.orig"
        if Path(orig).exists():
            import difflib
            old = Path(orig).read_text().splitlines()
            new = defconfig.read_text().splitlines()
            added = [l[1:] for l in difflib.unified_diff(old, new, lineterm="")
                     if l.startswith("+") and not l.startswith("+++")]
            if added:
                frag.write_text("\n".join(added) + "\n")
                shutil.copy2(orig, defconfig)
                Log.ok(f"fragment: {len(added)} 行")
            else:
                # defconfig 已被还原(续跑 step9), 保留现有 fragment 避免 features 丢失
                Log.warn("defconfig 与 .orig 相同 (续跑), 保留现有 fragment")
        # ===== 追加固定项 (rfkill/adios/ipset/KSU, .orig diff 不含这些) =====
        # 注: 必须追加在 .orig 机制之后, 否则被覆盖; 每次构建保证存在
        fixed_lines = ["CONFIG_KSU=y",
                       "CONFIG_KSU_NETISOLATE=y", "CONFIG_KSU_FUSEBPF_FIX=y",
                       "CONFIG_IP_SET=y", "CONFIG_IP_SET_BITMAP_IP=y",
                       "CONFIG_IP_SET_BITMAP_IPMAC=y", "CONFIG_IP_SET_BITMAP_PORT=y",
                       "CONFIG_IP_SET_HASH_IP=y", "CONFIG_IP_SET_HASH_IPMARK=y",
                       "CONFIG_IP_SET_HASH_IPPORT=y", "CONFIG_IP_SET_HASH_IPPORTIP=y",
                       "CONFIG_IP_SET_HASH_IPPORTNET=y", "CONFIG_IP_SET_HASH_MAC=y",
                       "CONFIG_IP_SET_HASH_NET=y", "CONFIG_IP_SET_HASH_NETIFACE=y",
                       "CONFIG_IP_SET_HASH_NETNET=y", "CONFIG_IP_SET_HASH_NETPORT=y",
                       "CONFIG_IP_SET_HASH_NETPORTNET=y", "CONFIG_IP_SET_LIST_SET=y",
                       "CONFIG_NETFILTER_XT_SET=y",
                      "CONFIG_MQ_IOSCHED_ADIOS=y", "CONFIG_MQ_IOSCHED_DEFAULT_ADIOS=y",
                      "CONFIG_MQ_IOSCHED_SSG=y",
                       ]
        if frag.exists():
            content = frag.read_text()
            for c in fixed_lines:
                if c not in content:
                    content += c + "\n"
            frag.write_text(content)
            Log.ok(f"固定项已追加 ({len(fixed_lines)} 项, 含 RFKILL=y)")
        frag_flag = ""
        if frag.exists():
            content = frag.read_text().strip()
            if content:
                frag_flag = f"--defconfig_fragment=//common:arch/arm64/configs/tesla.fragment"
        lto = "--lto=thin" if self.env.get("kernel_version") != "6.12" else "--lto=none"

        cmd = f"tools/bazel build --config=fast {lto} --action_env=KBUILD_BUILD_TIMESTAMP {frag_flag} //common:kernel_aarch64_dist"
        run(cmd, cwd=kr, timeout=10800, log_output=True)

        img = kr / "bazel-bin" / "common" / "kernel_aarch64" / "Image"
        if not img.exists():
            raise BuildError("Image 未生成")
        r = subprocess.run(["strings", str(img)], capture_output=True, text=True)
        ver = [l for l in r.stdout.splitlines() if "Linux version" in l]
        if ver:
            Log.ok(ver[0][:100])

    # ================= 10. 打包 =================
    def step10(self):
        Log.step(10, "打包 AnyKernel3")
        # 实时从内核 Makefile 读 SUBLEVEL (修复: 续跑/单步时 ACTUAL_SUBLEVEL 缺失 → 错名 6.6.77)
        mk = self.kernel_root / "common" / "Makefile"
        sub = None
        if mk.exists():
            for line in mk.read_text().splitlines():
                if line.startswith("SUBLEVEL = "):
                    sub = line.split("=")[1].strip()
                    break
        self.ACTUAL_SUBLEVEL = sub or "77"
        img = self.kernel_root / "bazel-bin" / "common" / "kernel_aarch64" / "Image"
        ak3 = ROOT / "third_party" / "AnyKernel3"
        out = ROOT / "out"
        out.mkdir(exist_ok=True)
        zip_name = f"android15-6.6.{self.ACTUAL_SUBLEVEL}-{self.env['os_patch_level']}-AnyKernel3.zip"
        staging = out / "ak3"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(ak3, staging, ignore=shutil.ignore_patterns(".git"))
        shutil.copy2(img, staging / "Image")
        run(f"cd {staging} && zip -q -r ../{zip_name} .")
        if not (out / zip_name).exists():
            raise BuildError(f"打包失败: {zip_name} 未生成")
        Log.ok(f"产物: out/{zip_name}")

    # ================= 运行 =================
    def run_all(self, start=0, only=None):
        def key(s):
            try:
                return float(s.id)
            except (TypeError, ValueError):
                return float("inf")
        if only is not None:
            steps = [s for s in self.steps if key(s) == float(only)]
        else:
            steps = [s for s in self.steps]
        for s in steps:
            if key(s) < float(start):
                continue
            try:
                s.fn()
            except BuildError as e:
                Log.err(str(e))
                Log.err(f"失败于步骤 [{s.id}] {s.title}")
                Log.info(f"续跑: python3 build.py --step {s.id}")
                sys.exit(1)
            except Exception as e:
                Log.err(f"异常: {e}")
                Log.err(f"失败于步骤 [{s.id}] {s.title}")
                Log.info(f"续跑: python3 build.py --step {s.id}")
                sys.exit(1)
        Log.info("\n构建流程结束")


def main():
    ap = argparse.ArgumentParser(description="Tesla_Kernel 构建器")
    ap.add_argument("--step", type=float, default=0, help="从指定步骤开始")
    ap.add_argument("--only", type=float, help="只执行指定步骤")
    args = ap.parse_args()

    cfg = load_config(CONFIG_FILE)
    b = Builder(cfg)
    b.register(Step(0, "构建环境准备", b.step00))
    b.register(Step(1, "准备工具链", b.step01))
    b.register(Step(2, "准备依赖仓库", b.step02))
    b.register(Step(3, "签名密钥 + git 配置", b.step03))
    b.register(Step(4, "恢复干净内核源码", b.step04))
    b.register(Step(5, "提取子版本号", b.step05))
    b.register(Step(5.5, "WiFi/蓝牙兼容性修复", b.step05b))
    b.register(Step(5.6, "添加 KernelSU (ReSukiSU)", b.step05c))
    b.register(Step(5.7, "KSU seccomp PF_EXITING 防死机", b.step05d))
    b.register(Step(5.8, "应用 SUSFS 补丁", b.step05e))
    b.register(Step(6, "配置内核选项 + defconfig 备份", b.step06))
    b.register(Step(6.5, "应用补丁 (模块化开关)", b.step06b))
    b.register(Step(7, "KMI 关闭 + setlocalversion", b.step07))
    b.register(Step(8, "构建时间戳", b.step08))
    b.register(Step(9, "编译内核 (bazel)", b.step09))
    b.register(Step(10, "打包 AnyKernel3", b.step10))

    Log.init_file(LOG_FILE)
    Log.info(f"日志: {LOG_FILE}")
    try:
        b.run_all(start=args.step, only=args.only)
    finally:
        Log._f.close() if Log._f else None


if __name__ == "__main__":
    main()
