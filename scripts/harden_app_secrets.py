"""Convert a DigitalOcean app's environment variables to encrypted SECRET type.

Audit finding E01, 26 October 2026: the backend app stored all 39 of its
environment variables as `type: GENERAL`, which DigitalOcean keeps in plaintext
in the app spec. That includes SECRET_KEY, DATABASE_URL, SPACES_KEY,
SPACES_SECRET, ANTHROPIC_API_KEY, WHATSAPP_ACCESS_TOKEN and SMTP_PASSWORD. Any
read-scoped API token retrieves every one of them in clear text.

`type: SECRET` makes DigitalOcean encrypt the value at rest and return it as
ciphertext (`EV[1:...]`) thereafter.

WHY THIS IS A SCRIPT AND NOT SOMETHING ALREADY DONE
    `doctl apps update --spec` replaces the *entire* spec. A mistake does not
    edit a field, it rewrites the app -- and there is no staging environment to
    find that out on. Applying it also triggers a redeploy, so the service
    restarts. That is a decision to make while watching, not to discover later.

WHAT IT DOES
    Reads the live spec, flips `type` to SECRET on the keys that hold secrets,
    leaves the rest alone, and writes the result to a file. It does NOT apply
    anything. Review the diff, then apply it yourself with the command it
    prints.

Usage (from onemarinex-backend/):
    PYTHONPATH=. python scripts/harden_app_secrets.py <APP_ID>
"""
import json
import subprocess
import sys

# Keys whose value is a credential. Everything else keeps its current type:
# a hostname or a feature flag gains nothing from encryption and loses
# readability in the console.
SECRET_KEYS = {
    "SECRET_KEY",
    "DATABASE_URL",
    "SPACES_KEY",
    "SPACES_SECRET",
    "ANTHROPIC_API_KEY",
    "WHATSAPP_ACCESS_TOKEN",
    "SMTP_PASSWORD",
}

OUT = "app-spec-hardened.json"


def _flip(envs):
    changed = []
    for e in envs or []:
        if e.get("key") in SECRET_KEYS and e.get("type") != "SECRET":
            e["type"] = "SECRET"
            changed.append(e["key"])
    return changed


def main(argv):
    if len(argv) != 1:
        sys.exit(__doc__.strip().splitlines()[-1])
    app_id = argv[0]

    raw = subprocess.run(
        ["doctl", "apps", "spec", "get", app_id, "--format", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    spec = json.loads(raw)

    changed = _flip(spec.get("envs"))
    for kind in ("services", "workers", "jobs", "static_sites"):
        for component in spec.get(kind) or []:
            changed += _flip(component.get("envs"))

    if not changed:
        print("Nothing to change: every credential is already type SECRET.")
        return 0

    with open(OUT, "w") as handle:
        json.dump(spec, handle, indent=2)

    print("Would encrypt %d value(s):" % len(changed))
    for key in sorted(set(changed)):
        print("  -", key)
    print()
    print("Written to %s. That file contains the current plaintext values," % OUT)
    print("so review it, apply it, then delete it:")
    print()
    print("    doctl apps update %s --spec %s" % (app_id, OUT))
    print("    rm %s" % OUT)
    print()
    print("Applying triggers a redeploy, so the service restarts. Watch it.")
    print("Afterwards the values are returned as EV[1:...] ciphertext, which")
    print("means you can no longer read them back -- keep your own copy first.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
