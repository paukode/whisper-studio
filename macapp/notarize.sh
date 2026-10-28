#!/usr/bin/env bash
# Notarize and staple one artifact: "dist-app/Whisper Studio.app" (before
# make_dmg.sh) or the DMG (after it). The same script runs in CI and on a
# laptop, so a local release and a CI release go through the same steps.
#
#   bash macapp/notarize.sh "dist-app/Whisper Studio.app"
#   bash macapp/notarize.sh dist-app/WhisperStudio-<version>.dmg
#
# Credentials, first match wins:
#   NOTARY_PROFILE   a notarytool keychain profile (local Macs), made once with
#                    xcrun notarytool store-credentials <name> --apple-id ... --team-id ...
#   APPLE_ID + APPLE_TEAM_ID + APPLE_APP_PASSWORD   (CI secrets; the password is
#                    an app-specific password from appleid.apple.com)
#
# Fails unless Apple answers "Accepted", and prints Apple's log on any other
# answer, so a rejected file is named instead of a release going out unstapled.
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

TARGET="${1:-}"
[[ -n "$TARGET" && -e "$TARGET" ]] || die "usage: notarize.sh <path to .app or .dmg>"

if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    AUTH=(--keychain-profile "$NOTARY_PROFILE")
elif [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
    AUTH=(--apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD")
else
    die "no notary credentials: set NOTARY_PROFILE, or APPLE_ID, APPLE_TEAM_ID and APPLE_APP_PASSWORD"
fi

# A notarized artifact must carry a real Developer ID signature with a
# secure timestamp; an ad-hoc build is refused here rather than by Apple.
codesign --verify --strict "$TARGET" || die "$TARGET is not validly signed"
codesign -dvv "$TARGET" 2>&1 | grep -q "^Authority=Developer ID Application" \
    || die "$TARGET is not signed with a Developer ID Application certificate"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# notarytool takes a zip, a dmg or a pkg: an .app goes up as a zip.
case "$TARGET" in
    *.app | *.app/)
        UPLOAD="$WORK/upload.zip"
        echo "==> Zipping $TARGET"
        ditto -c -k --keepParent "$TARGET" "$UPLOAD"
        ;;
    *.dmg) UPLOAD="$TARGET" ;;
    *) die "$TARGET is neither an .app nor a .dmg" ;;
esac

echo "==> Submitting $(basename "$TARGET") to Apple (waits for the verdict)"
RESULT="$WORK/result.json"
# A rejected submission still exits 0 here; the status below decides.
xcrun notarytool submit "$UPLOAD" "${AUTH[@]}" --wait --output-format json > "$RESULT" \
    || { cat "$RESULT" >&2 || true; die "notarytool submit failed"; }

field() { /usr/bin/plutil -extract "$1" raw -o - "$RESULT" 2>/dev/null || true; }
STATUS="$(field status)"
SUBMISSION="$(field id)"
echo "    submission $SUBMISSION: $STATUS"
if [[ "$STATUS" != "Accepted" ]]; then
    if [[ -n "$SUBMISSION" ]]; then
        echo "==> Apple's log for $SUBMISSION" >&2
        xcrun notarytool log "$SUBMISSION" "${AUTH[@]}" >&2 || true
    fi
    die "notarization of $TARGET ended with status '${STATUS:-unknown}'"
fi

echo "==> Stapling the ticket"
xcrun stapler staple "$TARGET"
xcrun stapler validate "$TARGET"

# Gatekeeper's own verdict, as a downloaded copy would get it.
case "$TARGET" in
    *.dmg) spctl --assess --type open --context context:primary-signature -vv "$TARGET" ;;
    *) spctl --assess --type execute -vv "$TARGET" ;;
esac
echo "==> Notarized: $TARGET"
