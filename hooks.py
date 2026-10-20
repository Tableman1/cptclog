"""Shell integration snippets.

Each shell is told to emit invisible OSC escape sequences (prefix ESC ] 633 ;)
at well-defined points.  The recorder strips them from what you see and uses
them to know exactly which command ran, when, where, and what it printed:

    A;<b64 cwd>;<b64 user@host>   prompt is about to be drawn
    B                             prompt finished drawing, user is typing
    C;<b64 cmd>                   user pressed Enter, this command is starting
    D;<exit status>               command finished (bash/zsh)
    E;<b64 cmd>;<start>;<end>;<ok>;<lastexit>
                                  command finished (PowerShell, reported at next prompt)
"""

POSIX_EDIT_HOOK = r'''# editor wrappers: snapshot files before, emit a diff marker (F) after
__cptc_editors="nano vim vi nvim vim.tiny micro emacs pico joe"
__cptc_edit() {
    local editor="$1"; shift
    local rd="cat"; case "$editor" in sudo\ *) rd="sudo cat";; esac
    local files="" a f i=0 st tmp
    for a in "$@"; do case "$a" in -*|+*) ;; *) files="$files
$a";; esac; done
    tmp=$(mktemp -d 2>/dev/null || mktemp -d -t cptc)
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        if [ -f "$f" ] && [ "$($rd -- "$f" 2>/dev/null | wc -c)" -lt 2000000 ]; then
            $rd -- "$f" > "$tmp/$i" 2>/dev/null
        fi
        i=$((i+1))
    done <<EOF
$files
EOF
    printf '\033]633;T\a'
    command $editor "$@"
    st=$?
    i=0
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        if { [ -f "$f" ] || [ -f "$tmp/$i" ]; } && ! { $rd -- "$f" 2>/dev/null | cmp -s -- "$tmp/$i" - 2>/dev/null; }; then
            printf '\033]633;F;%s;%s;%s\a' "$(__cptc_b64 "$f")" \
                "$([ -f "$tmp/$i" ] && base64 < "$tmp/$i" | tr -d '\n')" \
                "$([ -f "$f" ] && $rd -- "$f" 2>/dev/null | base64 | tr -d '\n')"
        fi
        i=$((i+1))
    done <<EOF
$files
EOF
    rm -rf "$tmp"
    return $st
}
for __e in $(printf '%s' "$__cptc_editors"); do   # $(…) splits in both bash and zsh
    command -v "$__e" >/dev/null 2>&1 && eval "$__e() { __cptc_edit $__e \"\$@\"; }"
done
unset __e
sudo() {
    case " $__cptc_editors " in
        *" $1 "*) local __e="$1"; shift; __cptc_edit "sudo $__e" "$@";;
        *) command sudo "$@";;
    esac
}
sudoedit() { __cptc_edit "sudo -e" "$@"; }
'''

BASH_HOOK = r'''# ---- cptclog shell integration (bash) ----
__cptc_b64() { printf '%s' "$1" | base64 2>/dev/null | tr -d '\n'; }
__cptc_who() { printf '%s@%s' "${USER:-$(id -un 2>/dev/null)}" "${HOSTNAME:-$(hostname 2>/dev/null)}"; }
__cptc_ps0() {
    # PS0 is expanded after the line is read but before it runs; history 1 is that line.
    printf '\033]633;C;%s\a' "$(__cptc_b64 "$(HISTTIMEFORMAT= history 1 | sed 's/^ *[0-9]* *//')")"
}
__cptc_precmd() {
    local __st=$?
    printf '\033]633;D;%s\a\033]633;A;%s;%s\a' "$__st" "$(__cptc_b64 "$PWD")" "$(__cptc_b64 "$(__cptc_who)")"
    return $__st
}
HISTCONTROL=
HISTIGNORE=
set -o history
shopt -s interactive_comments 2>/dev/null
PS0='$(__cptc_ps0)'"${PS0}"
case ";$PROMPT_COMMAND;" in *";__cptc_precmd;"*) ;; *) PROMPT_COMMAND="__cptc_precmd${PROMPT_COMMAND:+;$PROMPT_COMMAND}";; esac
PS1="${PS1}"'\[\033]633;B\a\]'
''' + POSIX_EDIT_HOOK + '''# ---- end cptclog ----
'''

BASH_RCFILE = r'''# cptclog bootstrap: load the user's normal startup files, then the hook.
[ -f /etc/bash.bashrc ] && . /etc/bash.bashrc
[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"
''' + BASH_HOOK

ZSH_HOOK = r'''# ---- cptclog shell integration (zsh) ----
__cptc_b64() { printf '%s' "$1" | base64 2>/dev/null | tr -d '\n'; }
__cptc_who() { printf '%s@%s' "${USER:-$(id -un 2>/dev/null)}" "${HOST:-$(hostname 2>/dev/null)}"; }
__cptc_preexec() { printf '\033]633;C;%s\a' "$(__cptc_b64 "$1")"; }
__cptc_precmd() {
    local __st=$?
    printf '\033]633;D;%s\a\033]633;A;%s;%s\a' "$__st" "$(__cptc_b64 "$PWD")" "$(__cptc_b64 "$(__cptc_who)")"
}
autoload -Uz add-zsh-hook
add-zsh-hook preexec __cptc_preexec
add-zsh-hook precmd __cptc_precmd
setopt interactive_comments
PROMPT_EOL_MARK=''   # zsh's "%" partial-line marker would otherwise leak into captured output
PS1="${PS1}"$'%{\e]633;B\a%}'
''' + POSIX_EDIT_HOOK + '''# ---- end cptclog ----
'''

ZSH_ZSHENV = r'''[ -f "$HOME/.zshenv" ] && . "$HOME/.zshenv"
'''

ZSH_ZSHRC = r'''[[ -o login ]] && [ -f "$HOME/.zprofile" ] && . "$HOME/.zprofile"
[ -f "$HOME/.zshrc" ] && . "$HOME/.zshrc"
''' + ZSH_HOOK

POWERSHELL_HOOK = r'''# ---- cptclog shell integration (PowerShell) ----
$global:__cptc_lastId = -1
if (-not (Test-Path variable:global:__cptc_origPrompt)) {
    $global:__cptc_origPrompt = $function:prompt
}
function global:__cptc_b64([string]$s) {
    if ($null -eq $s) { $s = '' }
    [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($s))
}
function global:prompt {
    $ok = $?
    $lec = $global:LASTEXITCODE
    $esc = [char]27; $bel = [char]7
    $h = Get-History -Count 1
    if ($h -and $h.Id -ne $global:__cptc_lastId) {
        $global:__cptc_lastId = $h.Id
        $start = $h.StartExecutionTime.ToString('o')
        $end = $h.EndExecutionTime.ToString('o')
        $okStr = if ($ok) { '1' } else { '0' }
        $lecStr = if ($null -eq $lec) { '' } else { "$lec" }
        [Console]::Write("$esc]633;E;$(__cptc_b64 $h.CommandLine);$start;$end;$okStr;$lecStr$bel")
    }
    $who = "$env:USERNAME@$env:COMPUTERNAME"
    [Console]::Write("$esc]633;A;$(__cptc_b64 (Get-Location).Path);$(__cptc_b64 $who)$bel")
    # Restore $? / $LASTEXITCODE visibility for the user's original prompt.
    $global:LASTEXITCODE = $lec
    $text = if ($global:__cptc_origPrompt) { & $global:__cptc_origPrompt } else { "PS $($executionContext.SessionState.Path.CurrentLocation)> " }
    if ($text -is [array]) { $text = $text -join '' }
    [Console]::Write("$esc]633;B$bel")
    return "$text"
}
# editor wrappers: snapshot files before, emit a diff marker (F) after
$global:__cptc_editors = @('nano', 'vim', 'vi', 'nvim', 'micro', 'notepad')
function global:__cptc_edit([string]$editor, [string[]]$a) {
    $esc = [char]27; $bel = [char]7
    if ($null -eq $a) { $a = @() }
    $files = @($a | Where-Object { $_ -and -not $_.StartsWith('-') -and -not $_.StartsWith('+') -and -not $_.StartsWith('/') })
    $snap = @{}
    foreach ($f in $files) {
        if (Test-Path -LiteralPath $f -PathType Leaf) {
            try { $p = (Resolve-Path -LiteralPath $f).Path; if ((Get-Item -LiteralPath $p).Length -lt 2000000) { $snap[$f] = [IO.File]::ReadAllBytes($p) } } catch {}
        }
    }
    [Console]::Write("$esc]633;T$bel")
    if ($editor -eq 'notepad') {
        if ($a.Count) { Start-Process notepad.exe -ArgumentList $a -Wait } else { Start-Process notepad.exe -Wait }
    } else {
        $exe = (Get-Command $editor -CommandType Application | Select-Object -First 1).Source
        & $exe @a
    }
    $st = $global:LASTEXITCODE
    foreach ($f in $files) {
        $before = $snap[$f]; $after = $null
        if (Test-Path -LiteralPath $f -PathType Leaf) { try { $after = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $f).Path) } catch {} }
        if ($null -eq $before -and $null -eq $after) { continue }
        $same = ($null -ne $before -and $null -ne $after -and [Linq.Enumerable]::SequenceEqual([byte[]]$before, [byte[]]$after))
        if (-not $same) {
            $b = if ($before) { [Convert]::ToBase64String($before) } else { '' }
            $c = if ($after) { [Convert]::ToBase64String($after) } else { '' }
            [Console]::Write("$esc]633;F;$(__cptc_b64 $f);$b;$c$bel")
        }
    }
    $global:LASTEXITCODE = $st
}
foreach ($e in $global:__cptc_editors) {
    if ($e -eq 'notepad' -or (Get-Command $e -CommandType Application -ErrorAction SilentlyContinue)) {
        Invoke-Expression "function global:$e { __cptc_edit '$e' `$args }"
    }
}
# ---- end cptclog ----
'''

HOOKS = {"bash": BASH_HOOK, "zsh": ZSH_HOOK, "powershell": POWERSHELL_HOOK}
