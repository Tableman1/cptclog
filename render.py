"""Render a JSONL session log to a standalone HTML report."""
import difflib
import html
from recorder import read_log, verify

CSS = """
:root{--bg:#0f1115;--panel:#171a21;--fg:#d6d9e0;--dim:#8b919e;--ok:#3fb950;--bad:#f85149;
--warn:#d29922;--mod:#ff7b72;--add:#1f6f3a;--del:#7a2a2a;--line:#2a2f3a}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-monospace,Consolas,Menlo,monospace}
header{padding:16px 24px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{margin:0 0 6px;font-size:18px}header .meta{color:var(--dim);font-size:12px}
.banner{padding:10px 24px;font-weight:bold}.banner.ok{background:#12361f;color:var(--ok)}
.banner.bad{background:#4a1b1b;color:var(--mod)}
.tools{padding:10px 24px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.tools input{background:var(--panel);color:var(--fg);border:1px solid var(--line);padding:6px 8px;width:320px}
.tools label{color:var(--dim);font-size:12px}
.legend span{display:inline-block;padding:1px 8px;margin-right:6px;border-radius:3px;font-size:12px}
main{padding:0 24px 40px}
.entry{border:1px solid var(--line);border-radius:6px;margin:14px 0;background:var(--panel);overflow:hidden}
.entry.modified{border-color:var(--mod);box-shadow:0 0 0 1px var(--mod)}
.entry.failed .cmd{border-left:4px solid var(--bad)}
.entry.hidden{display:none}
.head{display:flex;gap:14px;flex-wrap:wrap;padding:6px 12px;font-size:12px;color:var(--dim);border-bottom:1px solid var(--line)}
.head .n{color:var(--fg);font-weight:bold}.head .exit.ok{color:var(--ok)}.head .exit.bad{color:var(--bad)}
.head .tag{background:var(--mod);color:#000;padding:0 6px;border-radius:3px;font-weight:bold}
.cmd{padding:8px 12px;white-space:pre-wrap;word-break:break-all;color:#e6edf3;font-weight:bold;border-left:4px solid var(--ok)}
.cmd::before{content:"$ ";color:var(--dim)}
pre{margin:0;padding:8px 12px;white-space:pre-wrap;word-break:break-all;max-height:600px;overflow:auto;border-top:1px dashed var(--line)}
pre .add{background:var(--add);display:block}pre .del{background:var(--del);display:block;text-decoration:line-through}
.empty{color:var(--dim);font-style:italic}
.chg{border-top:1px solid var(--line)}.chg .chead{padding:6px 12px;font-size:12px;color:var(--warn)}
.chg .chead b{color:var(--fg)}.chg pre{border-top:none}.chg .ctx{color:var(--dim)}
"""

JS = """
const q=document.getElementById('q'),onlyFail=document.getElementById('onlyfail');
function apply(){const s=q.value.toLowerCase();
document.querySelectorAll('.entry').forEach(e=>{const t=e.innerText.toLowerCase();
const show=(!s||t.includes(s))&&(!onlyFail.checked||e.classList.contains('failed'));
e.classList.toggle('hidden',!show)})}
q.addEventListener('input',apply);onlyFail.addEventListener('change',apply);
"""


def _esc(s):
    return html.escape(s if s is not None else "")


def _diff_html(old, new):
    """Colour lines that differ from the original: removed = red, added = green."""
    out = []
    for line in difflib.ndiff(old.split("\n"), new.split("\n")):
        tag, body = line[:2], line[2:]
        if tag == "  ":
            out.append(_esc(body))
        elif tag == "- ":
            out.append('<span class="del">%s</span>' % _esc(body))
        elif tag == "+ ":
            out.append('<span class="add">%s</span>' % _esc(body))
    return "\n".join(out)


def md_header(session):
    return "# cptclog session %s@%s\n\nstarted %s · shell %s · %s\n\n" % (
        session.get("user", ""), session.get("hostname", ""), session.get("started", ""),
        session.get("shell", ""), session.get("os", ""))


def md_entry(e):
    """One command as a markdown block (also used live while recording)."""
    t = (e.get("started") or "")[11:19]
    exit_txt = e["exit"] if e.get("exit") is not None else "?"
    out = e.get("output") or ""
    fence = "````" if "```" in out else "```"
    body = "%s\n$ %s\n%s%s\n" % (fence, e.get("command", ""), out + "\n" if out else "", fence)
    for ch in e.get("changes") or []:
        body += "\n**%s `%s`**\n\n```diff\n%s\n```\n" % (ch.get("status", "modified"), ch.get("path", ""), ch.get("diff", ""))
    return "## %d  %s  %s:%s  exit %s\n\n%s\n" % (e["seq"], t, e.get("host", ""), e.get("cwd", ""), exit_txt, body)


def render_md(log_path, out_path):
    entries = read_log(log_path)
    session = next((e for e in entries if e.get("type") == "session"), {})
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(md_header(session))
        for e in entries:
            if e.get("type") == "cmd":
                fh.write(md_entry(e))
    return sum(1 for e in entries if e.get("type") == "cmd")


def _diff_lines(diff):
    out = []
    for line in diff.split("\n"):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            out.append('<span class="ctx">%s</span>' % _esc(line))
        elif line.startswith("+"):
            out.append('<span class="add">%s</span>' % _esc(line))
        elif line.startswith("-"):
            out.append('<span class="del" style="text-decoration:none">%s</span>' % _esc(line))
        else:
            out.append(_esc(line))
    return "\n".join(out)


def render(log_path, out_path, against=None):
    entries = read_log(log_path)
    checks = verify(entries)
    session = next((e for e in entries if e.get("type") == "session"), {})
    cmds = [e for e in entries if e.get("type") == "cmd"]
    originals = {}
    if against:
        originals = {e["seq"]: e for e in read_log(against) if e.get("type") == "cmd"}

    modified = [s for s, ok in checks.items() if not ok]
    parts = ["<!doctype html><html><head><meta charset='utf-8'><title>cptclog %s</title>"
             "<style>%s</style></head><body>" % (_esc(session.get("hostname", "")), CSS)]
    parts.append("<header><h1>cptclog session &mdash; %s@%s</h1><div class='meta'>started %s &middot; shell %s &middot; %s &middot; %d commands &middot; source %s</div></header>"
                 % (_esc(session.get("user")), _esc(session.get("hostname")), _esc(session.get("started")),
                    _esc(session.get("shell")), _esc(session.get("os")), len(cmds), _esc(log_path)))
    if modified:
        parts.append("<div class='banner bad'>INTEGRITY CHECK FAILED &mdash; %d entr%s no longer match%s the recorded hash chain (highlighted in red): %s</div>"
                     % (len(modified), "y" if len(modified) == 1 else "ies", "es" if len(modified) == 1 else "", ", ".join("#%d" % s for s in modified)))
    else:
        parts.append("<div class='banner ok'>Integrity check passed &mdash; every entry matches its recorded hash.</div>")
    parts.append("<div class='tools'><input id='q' placeholder='filter commands / output'> "
                 "<label><input type='checkbox' id='onlyfail'> only non-zero exit</label>"
                 "<span class='legend'><span style='background:var(--mod);color:#000'>modified entry</span>")
    if against:
        parts.append("<span style='background:var(--add)'>added vs original</span><span style='background:var(--del)'>removed vs original</span>")
    parts.append("</span></div><main>")

    for e in cmds:
        ok = checks.get(e["seq"], True)
        failed = e.get("exit") not in (0, None)
        cls = "entry" + (" modified" if not ok else "") + (" failed" if failed else "")
        exit_cls = "ok" if e.get("exit") == 0 else ("bad" if failed else "")
        exit_txt = "exit %s" % e["exit"] if e.get("exit") is not None else "exit ?"
        dur = ""
        try:
            from datetime import datetime
            a = datetime.fromisoformat(e["started"]); b = datetime.fromisoformat(e["finished"])
            dur = " (%.1fs)" % (b - a).total_seconds()
        except Exception:
            pass
        parts.append("<div class='%s' id='c%d'><div class='head'><span class='n'>#%d</span>"
                     "<span>%s%s</span><span>%s</span><span>%s</span><span class='exit %s'>%s</span>%s</div>"
                     % (cls, e["seq"], e["seq"], _esc(e.get("started")), dur, _esc(e.get("host")),
                        _esc(e.get("cwd")), exit_cls, exit_txt,
                        "<span class='tag'>MODIFIED AFTER CAPTURE</span>" if not ok else ""))
        cmd_html = _esc(e.get("command"))
        orig = originals.get(e["seq"])
        if orig and orig.get("command") != e.get("command"):
            cmd_html = _diff_html(orig.get("command", ""), e.get("command", ""))
        parts.append("<div class='cmd'>%s</div>" % cmd_html)
        output = e.get("output") or ""
        if orig and orig.get("output", "") != output:
            parts.append("<pre>%s</pre>" % _diff_html(orig.get("output", ""), output))
        elif output:
            parts.append("<pre>%s</pre>" % _esc(output))
        else:
            parts.append("<pre class='empty'>(no output)</pre>")
        for ch in e.get("changes") or []:
            parts.append("<div class='chg'><div class='chead'>file %s: <b>%s</b></div><pre>%s</pre></div>"
                         % (_esc(ch.get("status", "modified")), _esc(ch.get("path")), _diff_lines(ch.get("diff", ""))))
        parts.append("</div>")
    parts.append("</main><script>%s</script></body></html>" % JS)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
    return len(cmds), modified
