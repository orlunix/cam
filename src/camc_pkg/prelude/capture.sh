# Fast capture prelude hook.
#
# Protocol (return codes):
#   0  handled success
#   1  not handled / fall back to Python (preserve "$@")
#   2  usage error
#   *  handled failure (propagate to caller)
#
# Invoked as: _camc_prelude_capture "$@"

_camc_prelude_trim_trailing_ws() {
    while :; do
        case "$_camc_out" in
            *" ") _camc_out=${_camc_out%?} ;;
            *"	") _camc_out=${_camc_out%?} ;;
            *) break ;;
        esac
    done
}

_camc_prelude_same_host() {
    _camc_left="$1"
    _camc_right="$2"
    if [ -z "$_camc_left" ] || [ -z "$_camc_right" ]; then return 0; fi
    if [ "$_camc_left" = "$_camc_right" ]; then return 0; fi
    _camc_left_short=${_camc_left%%.*}
    _camc_right_short=${_camc_right%%.*}
    [ "$_camc_left_short" = "$_camc_right_short" ]
}

_camc_prelude_default_socket() {
    _camc_session_for_socket="$1"
    [ -n "$_camc_session_for_socket" ] || return 1
    for _camc_sock_dir in "/tmp/cam-sockets" "/tmp/cam-agent-sockets" "${HOME}/.local/share/cam/sockets"; do
        _camc_candidate="${_camc_sock_dir}/${_camc_session_for_socket}.sock"
        if [ -e "$_camc_candidate" ]; then
            printf "%s" "$_camc_candidate"
            return 0
        fi
    done
    return 1
}

_camc_prelude_lookup_agent_json() {
    _camc_want="$1"
    _camc_agents="${HOME}/.cam/agents.json"
    [ -r "$_camc_agents" ] || return 1
    awk -v want="$_camc_want" '
      function val(line) {
        sub(/^[[:space:]]*"[^"]+":[[:space:]]*"/, "", line)
        sub(/",?[[:space:]]*$/, "", line)
        gsub(/\\"/, "\"", line)
        gsub(/\\\\/, "\\", line)
        return line
      }
      function reset() { hit=0; host=""; session=""; socket=""; tmux="" }
      BEGIN { in_agent=0; reset() }
      /^  [{]$/ { in_agent=1; reset(); next }
      in_agent {
        if ($0 ~ /^[[:space:]]*"id":[[:space:]]*"/) {
          if (val($0) == want) hit=1
        } else if ($0 ~ /^[[:space:]]*"hostname":[[:space:]]*"/) {
          host=val($0)
        } else if ($0 ~ /^[[:space:]]*"tmux_session":[[:space:]]*"/) {
          session=val($0)
        } else if ($0 ~ /^[[:space:]]*"session":[[:space:]]*"/) {
          if (session == "") session=val($0)
        } else if ($0 ~ /^[[:space:]]*"tmux_socket":[[:space:]]*"/) {
          socket=val($0)
        } else if ($0 ~ /^[[:space:]]*"tmux_bin":[[:space:]]*"/) {
          tmux=val($0)
        }
        if ($0 ~ /^  [}],?$/) {
          if (hit && session != "") {
            print host "\t" session "\t" socket "\t" tmux
            exit 0
          }
          in_agent=0
          reset()
        }
      }
    ' "$_camc_agents"
}

_camc_prelude_send_text_to_tmux() {
    _camc_session="$1"
    _camc_socket="$2"
    _camc_tmux="$3"
    _camc_text="$4"
    _camc_send_enter="$5"
    _camc_target="${_camc_session}:0.0"

    if [ "$_camc_tmux" != "tmux" ] && [ ! -x "$_camc_tmux" ]; then
        echo "tmux binary not found: $_camc_tmux" >&2
        return 1
    fi
    if [ -n "$_camc_socket" ] && [ ! -e "$_camc_socket" ]; then
        echo "tmux socket not found: $_camc_socket" >&2
        return 1
    fi

    if [ -n "$_camc_socket" ]; then
        "$_camc_tmux" -u -S "$_camc_socket" send-keys -t "$_camc_target" -l -- "$_camc_text"
    else
        "$_camc_tmux" -u send-keys -t "$_camc_target" -l -- "$_camc_text"
    fi
    _camc_rc=$?
    if [ "$_camc_rc" -ne 0 ]; then return "$_camc_rc"; fi

    if [ "$_camc_send_enter" = "1" ]; then
        sleep 0.15 2>/dev/null || sleep 1
        if [ -n "$_camc_socket" ]; then
            "$_camc_tmux" -u -S "$_camc_socket" send-keys -t "$_camc_target" Enter
        else
            "$_camc_tmux" -u send-keys -t "$_camc_target" Enter
        fi
        _camc_rc=$?
        if [ "$_camc_rc" -ne 0 ]; then return "$_camc_rc"; fi
    fi

    printf "Sent.\n"
    return 0
}

_camc_prelude_capture() {
    if [ "$1" = "send" ]; then
        shift
        _camc_id=""
        _camc_text=""
        _camc_have_text=0
        _camc_send_enter=1
        _camc_unsafe=0
        while [ $# -gt 0 ]; do
            case "$1" in
                --text|-t)
                    shift
                    if [ $# -eq 0 ]; then echo "--text requires a value" >&2; return 2; fi
                    if [ "$_camc_have_text" = "1" ]; then _camc_unsafe=1; break; fi
                    _camc_text="$1"
                    _camc_have_text=1
                    ;;
                --no-enter)
                    _camc_send_enter=0
                    ;;
                --no-fast-path)
                    return 1
                    ;;
                --file|-f|--stdin)
                    _camc_unsafe=1
                    ;;
                --*)
                    _camc_unsafe=1
                    ;;
                *)
                    if [ -z "$_camc_id" ]; then _camc_id="$1"; else _camc_unsafe=1; fi
                    ;;
            esac
            shift
        done
        if [ "$_camc_unsafe" = "1" ] || [ "$_camc_have_text" != "1" ] || [ -z "$_camc_id" ]; then
            return 1
        fi
        _camc_ok=0
        if [ "${#_camc_id}" -eq 8 ]; then
            case "$_camc_id" in
                *[!0-9a-fA-F]*) _camc_ok=0 ;;
                *) _camc_ok=1 ;;
            esac
        fi
        if [ "$_camc_ok" != "1" ]; then return 1; fi
        case "$_camc_text" in
            *'
'*) return 1 ;;
        esac
        if [ "${#_camc_text}" -gt 8000 ]; then return 1; fi

        _camc_meta=$(_camc_prelude_lookup_agent_json "$_camc_id" 2>/dev/null || true)
        if [ -z "$_camc_meta" ]; then return 1; fi
        _camc_host=$(printf '%s' "$_camc_meta" | cut -f1)
        _camc_session=$(printf '%s' "$_camc_meta" | cut -f2)
        _camc_socket=$(printf '%s' "$_camc_meta" | cut -f3)
        _camc_tmux=$(printf '%s' "$_camc_meta" | cut -f4)
        _camc_myhost=$(hostname 2>/dev/null || uname -n)
        if ! _camc_prelude_same_host "$_camc_host" "$_camc_myhost" || [ -z "$_camc_session" ]; then
            return 1
        fi
        if [ -z "$_camc_socket" ]; then
            _camc_socket=$(_camc_prelude_default_socket "$_camc_session" 2>/dev/null || true)
        fi
        if [ -z "$_camc_tmux" ]; then
            if [ -x /bin/tmux ]; then _camc_tmux="/bin/tmux"; else _camc_tmux="tmux"; fi
        fi
        _camc_prelude_send_text_to_tmux "$_camc_session" "$_camc_socket" "$_camc_tmux" "$_camc_text" "$_camc_send_enter"
        return "$?"
    fi

    [ "$1" = "capture" ] || return 1

    _camc_json=0
    _camc_no_fast_path=0
    for _camc_arg in "$@"; do
        if [ "$_camc_arg" = "--json" ]; then _camc_json=1; fi
        if [ "$_camc_arg" = "--no-fast-path" ]; then _camc_no_fast_path=1; fi
    done

    if [ "$_camc_json" != "1" ] && [ "$_camc_no_fast_path" != "1" ]; then
        if (
            shift
            _camc_id=""
            _camc_lines="0"
            _camc_format="plain"
            _camc_unsafe=0
            while [ $# -gt 0 ]; do
                case "$1" in
                    --lines|-n)
                        shift
                        if [ $# -eq 0 ]; then _camc_unsafe=1; break; fi
                        _camc_lines="$1"
                        ;;
                    --format)
                        shift
                        if [ $# -eq 0 ]; then _camc_unsafe=1; break; fi
                        _camc_format="$1"
                        ;;
                    --*)
                        _camc_unsafe=1
                        ;;
                    *)
                        if [ -z "$_camc_id" ]; then _camc_id="$1"; else _camc_unsafe=1; fi
                        ;;
                esac
                shift
            done
            if [ "$_camc_unsafe" != "1" ] && [ -n "$_camc_id" ]; then
                _camc_ok=0
                if [ "${#_camc_id}" -eq 8 ]; then
                    case "$_camc_id" in
                        *[!0-9a-fA-F]*) _camc_ok=0 ;;
                        *) _camc_ok=1 ;;
                    esac
                fi
                if [ "$_camc_ok" = "1" ]; then
                    _camc_meta=$(_camc_prelude_lookup_agent_json "$_camc_id" 2>/dev/null || true)
                    if [ -n "$_camc_meta" ]; then
                        _camc_host=$(printf '%s' "$_camc_meta" | cut -f1)
                        _camc_session=$(printf '%s' "$_camc_meta" | cut -f2)
                        _camc_socket=$(printf '%s' "$_camc_meta" | cut -f3)
                        _camc_tmux=$(printf '%s' "$_camc_meta" | cut -f4)
                        _camc_myhost=$(hostname 2>/dev/null || uname -n)
                        if _camc_prelude_same_host "$_camc_host" "$_camc_myhost" && [ -n "$_camc_session" ]; then
                            if [ -z "$_camc_socket" ]; then
                                _camc_socket=$(_camc_prelude_default_socket "$_camc_session" 2>/dev/null || true)
                            fi
                            if [ -z "$_camc_tmux" ]; then
                                if [ -x /bin/tmux ]; then _camc_tmux="/bin/tmux"; else _camc_tmux="tmux"; fi
                            fi
                            _camc_host_ok=1
                            case "$_camc_format" in plain|ansi) ;; *) _camc_host_ok=0 ;; esac
                            case "$_camc_lines" in ''|*[!0-9-]*) _camc_host_ok=0 ;; esac
                            if [ "$_camc_host_ok" = "1" ]; then
                                if [ "$_camc_lines" -le 0 ]; then _camc_start="-"; else _camc_start="-$_camc_lines"; fi
                                if [ "$_camc_tmux" = "tmux" ] || [ -x "$_camc_tmux" ]; then
                                    if [ -z "$_camc_socket" ] || [ -e "$_camc_socket" ]; then
                                        if [ -n "$_camc_socket" ]; then
                                            if [ "$_camc_format" = "ansi" ]; then
                                                _camc_out=$("$_camc_tmux" -u -S "$_camc_socket" capture-pane -p -J -e -t "$_camc_session:0.0" -S "$_camc_start")
                                            else
                                                _camc_out=$("$_camc_tmux" -u -S "$_camc_socket" capture-pane -p -J -t "$_camc_session:0.0" -S "$_camc_start")
                                            fi
                                        else
                                            if [ "$_camc_format" = "ansi" ]; then
                                                _camc_out=$("$_camc_tmux" -u capture-pane -p -J -e -t "$_camc_session:0.0" -S "$_camc_start")
                                            else
                                                _camc_out=$("$_camc_tmux" -u capture-pane -p -J -t "$_camc_session:0.0" -S "$_camc_start")
                                            fi
                                        fi
                                        _camc_rc=$?
                                        if [ "$_camc_rc" -eq 0 ]; then
                                            _camc_prelude_trim_trailing_ws
                                            printf "%s" "$_camc_out"
                                            exit 0
                                        fi
                                    fi
                                fi
                            fi
                        fi
                    fi
                fi
            fi
            exit 1
        ); then
            return 0
        fi
    fi

    return 1
}
