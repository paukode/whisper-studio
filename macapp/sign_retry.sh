# Sourced by build_app.sh and make_dmg.sh.
#
# codesign_retry <codesign arguments>: sign, and try again when Apple's
# timestamp service did not answer. A Developer ID signature needs a secure
# timestamp from that service, and one unanswered request fails the whole
# build ("The timestamp service is not available.", "A timestamp was expected
# but was not found."). Any other failure is final at once. Quiet on success;
# the output of a failed call goes to stderr.
#
# SIGN_RETRY_WAIT: seconds before the second attempt (default 15); each
# later wait is one step longer (15, 30, 45, 60).
codesign_retry() {
    local attempt out wait_s="${SIGN_RETRY_WAIT:-15}"
    for attempt in 1 2 3 4 5; do
        if out="$(codesign "$@" 2>&1)"; then return 0; fi
        printf '%s\n' "$out" >&2
        case "$out" in
            *[Tt]imestamp*) ;;
            *) return 1 ;;
        esac
        [[ "$attempt" -lt 5 ]] || break
        echo "    the timestamp service did not answer (attempt $attempt of 5); retrying in $((attempt * wait_s)) s" >&2
        sleep $((attempt * wait_s))
    done
    echo "    the timestamp service did not answer 5 times" >&2
    return 1
}
