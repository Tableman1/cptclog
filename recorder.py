"""Marker parsing, output cleaning, and the hash-chained JSONL log."""
import base64
import difflib
import datetime as _dt
import hashlib
import json
import re
import socket
import getpass
import platform

OSC_PREFIX = "\x1b]633;"
_OSC_RE = re.compile(r"\x1b\]633;(.*?)(?:\x07|\x1b\\)", re.S)

# Everything we strip when turning raw terminal bytes into readable text.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"          # CSI sequences (colors, cursor moves ...)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # other OSC sequences (titles ...)
    r"|\x1b[PX^_].*?\x1b\\"             # DCS/SOS/PM/APC strings
    r"|\x1b[@-Z\\-_]"                   # 2-byte escapes
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # stray control chars (keep \t \n \r)
    , re.S)


def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def b64d(s):
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except Exception:
        return s


def clean_output(text):
    """Strip escape codes and resolve carriage-return redraws (progress bars)."""
    text = _ANSI_RE.sub("", text)
    out = []
    for line in text.split("\n"):
        if "\r" in line:
            parts = line.split("\r")
            # keep the last segment that actually has content
            line = next((p for p in reversed(parts) if p.strip()), parts[-1])
        out.append(line.rstrip())
    return "\n".join(out).strip("\n")


def entry_hash(entry, prev_hash):
    keys = ["seq", "started", "finished", "host", "cwd", "command", "exit", "output"]
    if "changes" in entry:
        keys.append("changes")
    payload = json.dumps(
        {k: entry.get(k) for k in keys},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


class Recorder:
    """Feed it raw terminal output; it returns what to display and logs commands."""

    def __init__(self, path, shell, on_entry=None):
        self.path = path
        self.on_entry = on_entry
        self.seq = 0
        self.prev_hash = "0" * 64
        self.cur = None          # entry currently executing
        self.outbuf = []         # text since last B/C marker
        self.pending_cwd = ""
        self.pending_host = ""
        self.pending_changes = []
        self.pending_tui = False
        self.hold = ""           # partial marker carried between reads
        self.fh = open(path, "a", encoding="utf-8")
        header = {
            "type": "session", "started": now_iso(), "shell": shell,
            "user": getpass.getuser(), "hostname": socket.gethostname(),
            "os": platform.platform(),
        }
        self._write(header)

    # -------- stream handling --------
    def feed(self, text):
        """Return the text to show on screen (markers removed)."""
        text = self.hold + text
        self.hold = ""
        # keep an incomplete trailing marker for the next read
        i = text.rfind("\x1b]")
        if i == -1:
            i = text.rfind("\x1b")
        if i != -1:
            tail = text[i:]
            is_partial_prefix = OSC_PREFIX.startswith(tail) or tail.startswith(OSC_PREFIX)
            if is_partial_prefix and not re.search(r"\x07|\x1b\\", tail) and len(tail) < 16_000_000:
                self.hold = tail
                text = text[:i]
        shown = []
        pos = 0
        for m in _OSC_RE.finditer(text):
            chunk = text[pos:m.start()]
            shown.append(chunk)
            self.outbuf.append(chunk)
            self._marker(m.group(1))
            pos = m.end()
        tail = text[pos:]
        shown.append(tail)
        self.outbuf.append(tail)
        return "".join(shown)

    def _marker(self, body):
        parts = body.split(";")
        kind = parts[0]
        if kind == "A":
            self._finalize()
            self.pending_cwd = b64d(parts[1]) if len(parts) > 1 else ""
            self.pending_host = b64d(parts[2]) if len(parts) > 2 else ""
        elif kind == "B":
            self.outbuf = []
        elif kind == "C":
            if self.cur is not None:        # nested shell (e.g. hook pasted into ssh)
                self.cur["output"] = self._out()
                self._finalize()
            self.cur = self._new_entry(b64d(parts[1]) if len(parts) > 1 else "")
            self.outbuf = []
        elif kind == "D":
            if self.cur is not None:
                self.cur["exit"] = _int(parts[1] if len(parts) > 1 else None)
                self.cur["finished"] = now_iso()
                self.cur["output"] = self._out()
        elif kind == "T":           # interactive/full-screen program: don't keep the screen scribble
            if self.cur is not None:
                self.cur["tui"] = True
            else:
                self.pending_tui = True
        elif kind == "F":
            change = file_change(parts[1] if len(parts) > 1 else "",
                                 parts[2] if len(parts) > 2 else "",
                                 parts[3] if len(parts) > 3 else "")
            if self.cur is not None:
                self.cur.setdefault("changes", []).append(change)
            else:
                self.pending_changes.append(change)
        elif kind == "E":
            # PowerShell: cmd;start;end;ok;lastexit — output is everything since B,
            # minus the echoed input line.
            cmd = b64d(parts[1]) if len(parts) > 1 else ""
            e = self._new_entry(cmd)
            if len(parts) > 2 and parts[2]:
                e["started"] = parts[2]
            if len(parts) > 3 and parts[3]:
                e["finished"] = parts[3]
            ok = parts[4] if len(parts) > 4 else "1"
            lec = _int(parts[5] if len(parts) > 5 else None)
            e["exit"] = lec if lec is not None else (0 if ok == "1" else 1)
            raw = clean_output("".join(self.outbuf))
            lines = raw.split("\n")
            if lines and _looks_like_echo(lines[0], cmd):
                lines = lines[1:]
            e["output"] = "\n".join(lines).strip("\n")
            if self.pending_tui:
                e["output"] = "(interactive program - screen output not kept)"
                self.pending_tui = False
            if self.pending_changes:
                e["changes"] = self.pending_changes
                self.pending_changes = []
            self.cur = e
            self.outbuf = []

    def _out(self):
        if self.cur is not None and self.cur.get("tui"):
            return "(interactive program - screen output not kept)"
        return clean_output("".join(self.outbuf))

    def _new_entry(self, cmd):
        self.seq += 1
        return {"type": "cmd", "seq": self.seq, "started": now_iso(), "finished": None,
                "host": self.pending_host, "cwd": self.pending_cwd,
                "command": cmd, "exit": None, "output": ""}

    def _finalize(self):
        if self.cur is None:
            return
        e = self.cur
        self.cur = None
        e.pop("tui", None)
        if e["finished"] is None:
            e["finished"] = now_iso()
        e["prev"] = self.prev_hash
        e["hash"] = entry_hash(e, self.prev_hash)
        self.prev_hash = e["hash"]
        self._write(e)
        if self.on_entry:
            self.on_entry(e)

    def close(self):
        if self.cur is not None:
            self.cur["output"] = self._out()
            self._finalize()
        self._write({"type": "end", "finished": now_iso(), "commands": self.seq})
        self.fh.close()

    def _write(self, obj):
        self.fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.fh.flush()


def file_change(path_b64, before_b64, after_b64):
    """Build a {path, status, diff} record from the before/after file contents."""
    path = b64d(path_b64)
    try:
        before = base64.b64decode(before_b64) if before_b64 else None
        after = base64.b64decode(after_b64) if after_b64 else None
    except Exception:
        return {"path": path, "status": "modified", "diff": "(could not decode file contents)"}
    status = "created" if before is None else ("deleted" if after is None else "modified")
    if (before and b"\0" in before) or (after and b"\0" in after):
        return {"path": path, "status": status, "binary": True,
                "diff": "Binary file %s (%d -> %d bytes)" % (status, len(before or b""), len(after or b""))}
    a = (before or b"").decode("utf-8", "replace").splitlines()
    b = (after or b"").decode("utf-8", "replace").splitlines()
    diff = "\n".join(difflib.unified_diff(a, b, fromfile=path + " (before)", tofile=path + " (after)", lineterm=""))
    return {"path": path, "status": status, "diff": diff}


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _looks_like_echo(line, cmd):
    line = line.strip()
    return bool(cmd) and (line.endswith(cmd.strip()) or cmd.strip().startswith(line[-20:]) if line else False)


def read_log(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def verify(entries):
    """Return {seq: True/False} — False means the entry no longer matches its hash."""
    prev = "0" * 64
    result = {}
    for e in entries:
        if e.get("type") != "cmd":
            continue
        ok = e.get("prev") == prev and e.get("hash") == entry_hash(e, prev)
        result[e["seq"]] = ok
        prev = e.get("hash") or prev
    return result
