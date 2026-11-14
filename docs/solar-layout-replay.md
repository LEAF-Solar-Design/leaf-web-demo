# Solar saved-layout replay

Parent: Solar editable layouts in Leaf's shared CAD workspace. This regression
protects the S01 path after acceptance without reopening accepted projects.

Run the focused replay through the managed test runner:

```powershell
python C:/Users/ehaug/.claude/scripts/mini_node.py run --worktree <checkout> --kind pytest -- tests/test_solar_layout_replay.py -q
```

The serialization test uses only synthetic parameters. It generates one layout,
adds a second region with a different preset and pose, exports native DXF, and
reads saved frame pages. It checks that original frames retain their model,
each new frame uses the second model, and input hash and generation history
survive export. It does not depend on a frozen frame count or private drawing.

The HTTP test uses the existing verified disposable PostgreSQL producer. It
creates a new canonical project, publishes both generations, saves the exported
DXF through the editor route, reads the saved frame pages, then restores the
original bytes and attributes. Without the disposable producer it skips, which
is not evidence that the HTTP path passed. Native CI can supply that producer.
No live project, credential, licensed CAD runtime or staging release is needed.
The materialization suite calls the HTTP replay from its existing database gate,
so the selected CI path exercises it without changing shared CI configuration.

For a visible replay after a relevant staging change, use a new disposable
project. Record source identity, setup time and action time separately. Generate
from the original intake with the intended producer loading, take checkout
before selection, inspect frame attributes, save, reopen, make a bounded edit,
Undo and verify restoration. Capture the actual DXF download and its hash.
Switch Solar, CAD, Browser and Solar profiles within that same project. Release
only the checkout acquired by this replay and close its dedicated browser in
finally. Store screenshots and a receipt outside the public repository.

The HTTP regression proves the server transaction and saved bytes. A visible
replay proves UI selection, screen fit and shared profile continuity. Neither
replaces the other's evidence. Preserve earlier accepted recordings and their
separate continuation timings. Service sessions remain time limited; do not
extend authentication or weaken checkout authority to make a replay pass.
