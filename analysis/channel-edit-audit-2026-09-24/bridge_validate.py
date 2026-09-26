"""Bridge for the browser fix probes: validate ONE channel source (JSON on
stdin) with the real criteria module. Exit 0 always; the JSON answer carries
`valid` + typed errors — the Node harness turns those into real Apply
rejections instead of a mocked `valid: true` (audit E1 acceptance)."""
import json
import sys

sys.path.insert(0, sys.argv[1])

from justwatch import criteria  # noqa: E402

source = json.load(sys.stdin)
errors = criteria.validate(source)
print(json.dumps({"valid": not errors, "errors": errors}))
