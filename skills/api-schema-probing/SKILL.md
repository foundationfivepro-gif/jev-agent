---
name: api-schema-probing
description: Reverse-engineer an undocumented or mis-documented JSON API's request schema by reading its validation errors. Use when docs are missing or wrong, or a request keeps returning 400.
---

# Probing an API's schema from its errors

When docs are absent, stale or wrong, a well-validated API will tell you its own schema.
Modern validators emit the field path, the expected type and the received one. Each 400 is a
free hint, and this routinely beats searching — it is ground truth for the deployed version.

1. **Send something deliberately minimal.** A near-empty body surfaces required fields at once.
   An elaborate wrong payload yields one error; a bare one yields the skeleton.
2. **Change one thing per request**, or the error is ambiguous.
3. **Read the field path literally** before theorising.
4. **Let discriminators enumerate themselves** — omitting a type field often returns the full
   list of valid variants.
5. **Expect per-variant shapes.** A field named the same across variants may take a different
   type in each. Probe every variant.
6. **Stop at first success, then re-run with all variants together** to check they compose.

**Two error layers.** Schema errors (400, a field path) mean your shape is wrong — fix and
retry. Semantic errors (a named class like `ModelTypeMismatchError`) mean the shape parsed but
the request is wrong in kind — you are probably at the wrong endpoint; stop tweaking fields.

**Hygiene.** Never pipe `curl -w` output into a JSON parser — the status suffix appends to the
body and the parse fails with a confusing `Extra data` that looks like an API problem.
Truncate response bodies. Record the confirmed schema the moment it works: probing is paid
once, re-probing every session.

**Do not probe** endpoints that mutate state, cost real money per call, or are tightly rate
limited. Read-only or evaluation endpoints are ideal.
