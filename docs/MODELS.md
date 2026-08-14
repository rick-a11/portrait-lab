# Model and media setup

Portrait Lab keeps its application source separate from third-party model
weights and driving media. A normal Git checkout contains every first-party
source file, test, launcher, and setup script needed to reproduce the local
workspace. The setup scripts retrieve the remaining upstream materials from
their original publishers into ignored local paths.

## Automated GFPGAN setup

Run:

```bash
./scripts/bootstrap-gfpgan-macos.sh
```

The script creates an isolated Python 3.10 environment and downloads these
official release assets when they are not already present:

| Asset | Upstream source | Local destination |
| --- | --- | --- |
| `GFPGANv1.4.pth` | [TencentARC/GFPGAN v1.3.0](https://github.com/TencentARC/GFPGAN/releases/tag/v1.3.0) | `models/GFPGANv1.4.pth` |
| `detection_Resnet50_Final.pth` | [xinntao/facexlib v0.1.0](https://github.com/xinntao/facexlib/releases/tag/v0.1.0) | `.runtime/gfpgan/gfpgan/weights/` |
| `parsing_parsenet.pth` | [xinntao/facexlib v0.2.2](https://github.com/xinntao/facexlib/releases/tag/v0.2.2) | `.runtime/gfpgan/gfpgan/weights/` |

The API starts its GFPGAN process from the ignored `.runtime/gfpgan` directory,
so FaceXLib never performs an unexpected first-run download into an unrelated
working directory.

## Automated LivePortrait setup

Run:

```bash
./scripts/bootstrap-liveportrait-macos.sh
```

The script clones the official [KwaiVGI/LivePortrait](https://github.com/KwaiVGI/LivePortrait)
source into `.runtime/liveportrait`, installs its macOS requirements in an
independent Python 3.10 environment, and retrieves the required official
weights from [KlingTeam/LivePortrait](https://huggingface.co/KlingTeam/LivePortrait).
The named driving-video cards use the sample clips supplied by that upstream
source checkout. You may instead upload a video for which you have permission;
it is used only for the one local job and is removed afterwards.

## License and privacy boundary

Neither GFPGAN, FaceXLib, LivePortrait, their weights, nor their sample media
is redistributed by this repository. They remain subject to their original
licenses, notices, and acceptable-use restrictions. Review those terms before
commercial or production use, especially the upstream InsightFace model terms.

No account credentials, user photos, generated results, driving uploads,
deployment identifiers, or machine-specific paths belong in this repository.
