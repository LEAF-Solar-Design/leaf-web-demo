# Private video input for native source authoring

Parent: Studio18 I01 original-video recreation, inside the shared project source-author transaction.

This source slice adds a native-create input capability. It is not original-video acceptance or live runtime adoption. The worker admission permits source and synthetic tests only. It does not permit provider execution, image builds, live dependency installation or deployment.

## Existing transaction

The native adapter accepts one selected MOV with selected TXT/JSON transcript originals. Existing tenant, project, actor, digest, metadata freshness and cancellation checks still apply. Dispatch validation reads and verifies originals but never decodes or calls a model. Author execution decodes once before contacting the existing private author endpoint.

The decoder requires Linux, a non-root app worker, /usr/bin/ffmpeg, ffprobe, bubblewrap and prlimit. Missing tools or unavailable namespaces fail closed. There is no bare decoder fallback. prlimit bounds address space, CPU, file size, file descriptors and process count. Bubblewrap supplies private user, network and PID namespaces, a read-only runtime and original input, and one writable private output directory. The child receives no inherited credentials or user paths.

FFprobe counts decoded frames across the whole input. FFmpeg decodes the selected video stream and selects its first/last frames and frames at least two seconds apart. The manifest records actual selected PTS values and time base. Sampling is not every-frame visual inspection. Current ceilings are 180 seconds, 96 selected frames, 768 pixels per frame edge, 128 KiB per JPEG, 4 MiB aggregate JPEG bytes, and a 45-second decoder wall budget within the existing overall transaction budget. Oversized, incomplete or unconfirmed cleanup results cannot start source generation.

The harness checks frame bytes, manifest digest/identity and complete transcript bindings before grant use. A tool-free inspection uses the existing Agent SDK image-message transport and the existing acquired grant. Missing frame observations prevent the repository editor from starting. Manifest and observations become protected JSON resources beside the exact transcript bytes. The ordinary contained editor generates Swift. Both phases settle their reported token usage in the same existing lease. No provider fallback or retry is added.

Video input is currently limited to native create. Existing browser and text-only native create/edit inputs retain their behavior. The UI selection files belong to I02 integration and are not changed here. No new HTTP route is added.

## Canonical packaging and remaining runtime proof

The existing pinned apt stanza in deploy/Dockerfile.app now declares ffmpeg (which supplies ffprobe) and bubblewrap beside its existing util-linux package. It retains the signed update-digest checks, pinned git install prefix and existing USER 10003:10003 caller. Build-time executable checks cover all four fixed decoder tools. No second caller identity or privilege is introduced. The superseded standalone dependency installer is removed.

This worker has not built an image or installed production packages. The image owner still needs to prove that the actual runtime permits the required user/network/PID namespaces under the existing non-root caller. Installation alone does not prove containment. If those capabilities are absent, video remains visibly unavailable. No privileged-mode, host setting or bare-decoder fallback is permitted.

Root owns the exact-source image build, supply/containment proof with synthetic media, subsequent authorized original-video/provider execution, deployment, readiness and scored acceptance. Exact-head full native CI and canonical queue submission are worker-owned; these focused tests do not establish original I01 acceptance.

Decoder option sources: [FFmpeg MOV demuxer](https://ffmpeg.org/ffmpeg-formats.html#mov_002fmp4_002f3gp) and [FFprobe frame counting](https://ffmpeg.org/ffprobe.html). External data references and absolute paths are disabled and input protocols are restricted to file inside the namespace.

## Focused checks

From the repository root:

    python -P -m pytest -o pythonpath=server server/tests/test_project_video_materials.py server/tests/test_project_video_authoring.py server/tests/test_browser_project_authoring.py -q

From harness:

    npm test -- test/projectVideoInspection.test.ts test/projectSourceAuthoring.test.ts
    npm run typecheck

Fixtures are synthetic. The SDK and native process boundary are replaced by inert fakes in these tests. They establish wiring, provenance validation, resource/namespace arguments, cancellation, cleanup handling, protected inputs and failure behavior. They do not establish that a real deployed decoder cage or model consumed the original teacher video.
