# Applied by run_triage.sh to the WHOLE triage output (log lines, process arguments, the
# /health body, config sections), before it is shown or saved. GNU sed -E. Over-redacting a
# line is cheap; a credential in a Slack thread or a ticket is not.
#
# credentials in a URL:  rtsp://user:secret@host
s#(://)[^/@[:space:]]+:[^/@[:space:]]+@#\1<redacted>@#g
# command-line flags:  --password X, --token=X, --user X
s/(--(password|passwd|pass|pwd|token|secret|api[-_]?key|apikey|auth|credentials?|username|user)[= ])[^[:space:]]+/\1<redacted>/Ig
# bearer tokens
s/(Bearer[[:space:]]+)[A-Za-z0-9._~+\/=-]{6,}/\1<redacted>/Ig
# key: value / key=value / "key": "value", with an optional prefix or suffix on the key
# (camera_password, pw, pwd, api_key_secret, Authorization). The key must start at a word
# boundary, so "compass: north" is left alone.
s/(^|[^A-Za-z0-9])(([A-Za-z0-9]+[_-])*(passwords?|passwd|pass|pwd|pw|passphrase|token|secret|api_?key|apikey|credentials?|authorization|private_?key|psk)([_-][A-Za-z0-9]+)*["']?[[:space:]]*[:=][[:space:]]*).*/\1\2<redacted>/I
