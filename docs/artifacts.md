# Artifacts and evidence delivery

Artifacts are bounded, owner-bound evidence files produced by sandboxed runs: screenshots, Playwright traces and
application logs. They are stored on the control plane and retrieved through `artifact_get`. Nothing is served by
URL, and nothing is public.

## What is recorded

Each artifact has an opaque id (`ar-` plus 32 hex), the owning caller, the run, the workspace id and version, the
source revision, **the exact tested tree**, a media type, size, SHA-256, creation time and expiry. The runner's own
claims about an artifact (name, type, size, hash) are ignored: the control plane sniffs the type from the bytes and
recomputes size and hash.

## Delivery

| Class | Delivery |
|---|---|
| PNG, JPEG, WebP | **Native MCP image content** next to the structured result, never base64 inside JSON text. Above `ODYTOR_ARTIFACT_MAX_IMAGE_BYTES` (2 MiB) the image is described and offered in chunks. |
| Plain text, logs | Windows of up to 64 KiB (`offset`, `length`). |
| HTML, SVG, scripts | Stored, but returned only as **inert, labelled-untrusted text**; never as an image or a document. |
| Zip (traces) and binary | Bounded base64 chunks (48 KiB per call) with the whole-file SHA-256, for clients that cannot display images or for traces. |

The bounded base64 path is also the fallback for clients that do not render MCP image content. Odytor does not create
URLs for private repository artifacts.

## Limits and lifetime

Per artifact 16 MiB; per run 24 artifacts and 32 MiB; per caller 512 MiB; overall 4 GiB; 24 hours retention by default
(`ODYTOR_ARTIFACT_*`, `ODYTOR_ARTIFACTS=off` disables the store). Quotas apply per caller. Expired artifacts are
removed lazily and on access.

## Ownership and confinement

Only the submitting caller can read an artifact; unknown, foreign and expired ids give the same error. Ids are matched
against a strict pattern and used only as directory names under the store root; stored names are display labels and are
never used as paths.

## Verified

`tests/test_artifacts.py` (classification, quotas, ownership, expiry, windows) and
`tests/test_browser_e2e.py` (real screenshots delivered as image content through a real MCP client, retrieved again by
id, with the trace delivered as chunks). Whether a particular MCP **client** displays the image is client behavior and
is recorded in [client compatibility](client-compatibility.md) as tested or not tested.
