#!/usr/bin/env python3
"""cptclog - transparent terminal evidence logger for CPTC (Windows + Linux).

    python cptclog.py                 start a recorded shell (your default shell)
    python cptclog.py --shell bash    pick a shell: bash | zsh | powershell | pwsh
    python cptclog.py render LOG.jsonl [--against ORIGINAL.jsonl] [--md]
    python cptclog.py verify LOG.jsonl
    python cptclog.py hook bash       print the hook snippet (paste into a remote shell)
"""
import argparse
import codecs
import datetime as _dt
import os
import shutil
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hooks                      # noqa: E402
from recorder import Recorder, read_log, verify  # noqa: E402
from render import render, render_md, md_header, md_entry  # noqa: E402

IS_WIN = os.name == "nt"


def default_shell():
    if IS_WIN:
        return "pwsh" if shutil.which("pwsh") else "powershell"
    sh = os.path.basename(os.environ.get("SHELL", "bash"))
    return sh if sh in ("bash", "zsh") else "bash"


def log_paths(logdir):
    os.makedirs(logdir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(logdir, "%s_%s" % (socket.gethostname(), stamp))
    return base + ".jsonl", base + ".html", base + ".md"


def prepare_shell(shell, tmpdir):
    """Return (argv, env) that launches `shell` with the hook loaded."""
    env = dict(os.environ)
    env["CPTCLOG"] = "1"
    if shell == "bash":
        rc = os.path.join(tmpdir, "bashrc")
        with open(rc, "w") as fh:
            fh.write(hooks.BASH_RCFILE)
        return [shutil.which("bash") or "bash", "--rcfile", rc, "-i"], env
    if shell == "zsh":
        with open(os.path.join(tmpdir, ".zshenv"), "w") as fh:
            fh.write(hooks.ZSH_ZSHENV)
        with open(os.path.join(tmpdir, ".zshrc"), "w") as fh:
            fh.write(hooks.ZSH_ZSHRC)
        env["ZDOTDIR"] = tmpdir
        return [shutil.which("zsh") or "zsh", "-i"], env
    if shell in ("powershell", "pwsh"):
        ps1 = os.path.join(tmpdir, "cptclog_hook.ps1")
        with open(ps1, "w", encoding="utf-8-sig") as fh:
            fh.write(hooks.POWERSHELL_HOOK)
        exe = shutil.which(shell) or (shell + ".exe")
        return [exe, "-NoLogo", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", ps1], env
    sys.exit("unsupported shell: %s (use bash, zsh, powershell or pwsh)" % shell)


# ---------------------------------------------------------------- Linux / macOS
def run_posix(argv, env, rec):
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios
    import tty

    stdin_fd, stdout_fd = sys.stdin.fileno(), sys.stdout.fileno()

    def winsize():
        try:
            return struct.unpack("hhhh", fcntl.ioctl(stdout_fd, termios.TIOCGWINSZ, b"\0" * 8))
        except OSError:
            return (24, 80, 0, 0)

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(argv[0], argv, env)

    def set_size(*_):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("hhhh", *winsize()))
        except OSError:
            pass

    set_size()
    signal.signal(signal.SIGWINCH, set_size)
    old = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            try:
                r, _, _ = select.select([stdin_fd, fd], [], [])
            except InterruptedError:
                continue
            if fd in r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                shown = rec.feed(dec.decode(data))
                if shown:
                    os.write(stdout_fd, shown.encode("utf-8", "replace"))
            if stdin_fd in r:
                data = os.read(stdin_fd, 65536)
                if not data:
                    break
                os.write(fd, data)
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


# ---------------------------------------------------------------- Windows
def run_windows(argv, env, rec):
    import ctypes
    import ctypes.wintypes as wt
    import threading
    import time
    try:
        from winpty import PtyProcess
    except ImportError:
        sys.exit("cptclog needs the 'pywinpty' package on Windows:  pip install pywinpty")

    k32 = ctypes.windll.kernel32
    hin, hout = k32.GetStdHandle(-10), k32.GetStdHandle(-11)
    in_mode, out_mode = wt.DWORD(), wt.DWORD()
    k32.GetConsoleMode(hin, ctypes.byref(in_mode))
    k32.GetConsoleMode(hout, ctypes.byref(out_mode))
    # raw-ish input: no line buffering / echo / Ctrl-C processing, keys as VT sequences
    k32.SetConsoleMode(hin, (in_mode.value & ~(0x1 | 0x2 | 0x4)) | 0x200)
    # let our own output pass escape codes straight to the terminal
    k32.SetConsoleMode(hout, out_mode.value | 0x4 | 0x8)
    k32.SetConsoleOutputCP(65001)
    k32.SetConsoleCP(65001)

    def console_size():
        cols, rows = shutil.get_terminal_size((80, 24))
        return rows, cols

    rows, cols = console_size()
    proc = PtyProcess.spawn(argv, env=env, dimensions=(rows, cols))
    alive = True

    def pump_input():
        buf = ctypes.create_unicode_buffer(4096)
        n = wt.DWORD()
        while alive and proc.isalive():
            if not k32.ReadConsoleW(hin, buf, 4096, ctypes.byref(n), None):
                break
            if n.value:
                try:
                    proc.write(buf.value[:n.value])
                except Exception:
                    break

    def watch_resize():
        last = (rows, cols)
        while alive and proc.isalive():
            time.sleep(0.4)
            cur = console_size()
            if cur != last:
                last = cur
                try:
                    proc.setwinsize(*cur)
                except Exception:
                    pass

    threading.Thread(target=pump_input, daemon=True).start()
    threading.Thread(target=watch_resize, daemon=True).start()
    out = sys.stdout
    try:
        while True:
            try:
                data = proc.read(65536)
            except EOFError:
                break
            if not data:
                if not proc.isalive():
                    break
                continue
            shown = rec.feed(data)
            if shown:
                out.write(shown)
                out.flush()
    finally:
        alive = False
        k32.SetConsoleMode(hin, in_mode.value)
        k32.SetConsoleMode(hout, out_mode.value)


# ---------------------------------------------------------------- commands
def cmd_record(args):
    shell = args.shell or default_shell()
    jsonl, html_path, md_path = log_paths(args.logdir)
    tmpdir = tempfile.mkdtemp(prefix="cptclog_")
    argv, env = prepare_shell(shell, tmpdir)
    md = open(md_path, "a", encoding="utf-8")

    def on_entry(e):              # live markdown transcript: cat / tail -f it any time
        md.write(md_entry(e))
        md.flush()

    rec = Recorder(jsonl, shell, on_entry=on_entry)
    md.write(md_header(read_log(jsonl)[0]))
    md.flush()
    if not args.quiet:
        print("[cptclog] recording %s session -> %s  (live transcript: %s)" % (shell, jsonl, md_path), file=sys.stderr)
    try:
        if IS_WIN:
            run_windows(argv, env, rec)
        else:
            run_posix(argv, env, rec)
    finally:
        rec.close()
        md.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
        n, _ = render(jsonl, html_path)
        if not args.quiet:
            print("\n[cptclog] saved %d commands -> %s\n[cptclog] transcript -> %s\n[cptclog] report -> %s"
                  % (n, jsonl, md_path, html_path), file=sys.stderr)


def cmd_render(args):
    if args.md:
        out = args.out or os.path.splitext(args.log)[0] + ".md"
        print("rendered %d commands -> %s" % (render_md(args.log, out), out))
        return
    out = args.out or os.path.splitext(args.log)[0] + ".html"
    n, modified = render(args.log, out, against=args.against)
    print("rendered %d commands -> %s" % (n, out))
    if modified:
        print("WARNING: %d entries were modified after capture: %s" % (len(modified), modified))


def cmd_verify(args):
    checks = verify(read_log(args.log))
    bad = [s for s, ok in checks.items() if not ok]
    if bad:
        print("TAMPERED / EDITED entries: %s" % ", ".join("#%d" % s for s in bad))
        sys.exit(1)
    print("OK: all %d entries match the hash chain" % len(checks))


def cmd_hook(args):
    print(hooks.HOOKS[args.shell])


def main():
    p = argparse.ArgumentParser(prog="cptclog", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("record", help="start a recorded shell (default)")
    r.add_argument("--shell", choices=["bash", "zsh", "powershell", "pwsh"])
    r.add_argument("--logdir", default=os.path.join(HERE, "logs"))
    r.add_argument("-q", "--quiet", action="store_true", help="no start/stop banner lines")
    r.set_defaults(func=cmd_record)
    v = sub.add_parser("render", help="rebuild the HTML report from a .jsonl log")
    v.add_argument("log")
    v.add_argument("-o", "--out")
    v.add_argument("--against", help="original .jsonl to diff against; changes are coloured")
    v.add_argument("--md", action="store_true", help="write a markdown transcript instead of HTML")
    v.set_defaults(func=cmd_render)
    c = sub.add_parser("verify", help="check the hash chain of a .jsonl log")
    c.add_argument("log")
    c.set_defaults(func=cmd_verify)
    h = sub.add_parser("hook", help="print the shell hook for pasting into a remote shell")
    h.add_argument("shell", choices=sorted(hooks.HOOKS))
    h.set_defaults(func=cmd_hook)

    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["record"] + argv
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
