# Project photo inspection derivatives

Parent: shared Studio project materials and portfolio photo assignment.

Intake retains original bytes and their SHA256. Its isolated decoder creates a
JPEG preview at most 2048 pixels per edge and 1 MiB, reducing dimensions when
high-detail images exceed that byte budget. This applies to JPEG, PNG and HEIF
inputs. Preview processing does not change original dimensions or hashes in the
material record.

Inspection can also resize a previously stored preview that exceeds 1 MiB.
It never writes the resized bytes back to material storage. In an inspection
response, `sha256` and `preview_sha256` still identify the stored preview used
by catalog entries and saved assignments. A resized response adds both
`inspection_sha256` (the digest of returned bytes) and `derived_from_sha256`
(the stored preview digest).

The harness accepts these fields only as a pair, requires the source digest to
match its authorized catalog member, and verifies returned bytes against the
inspection digest. Existing responses without these fields must still hash to
the stored preview digest. Project, turn, source revision, original digest,
saved assignments and response-size checks apply to both forms. Conversion
failure returns the existing private error response and does not count the
photo as inspected.
