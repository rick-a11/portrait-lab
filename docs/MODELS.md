# Model setup

Portrait Lab intentionally does not distribute model weights, checkpoints, or
driving videos. Keeping them out of the repository makes the source checkout
small, prevents accidental redistribution, and lets every user review the
upstream terms before downloading.

## GFPGAN

Download `GFPGANv1.4.pth` from the
[official GFPGAN v1.3.0 release](https://github.com/TencentARC/GFPGAN/releases/tag/v1.3.0)
and place it at:

```text
models/GFPGANv1.4.pth
```

Then run `./scripts/bootstrap-gfpgan-macos.sh` to create the isolated Python
3.10 runtime.

## LivePortrait

Run `./scripts/bootstrap-liveportrait-macos.sh`. It clones the official
[KwaiVGI/LivePortrait](https://github.com/KwaiVGI/LivePortrait) repository into
an ignored local runtime and downloads only the required upstream weights.

The LivePortrait sample driving clips remain in that local upstream checkout.
Use them only under their upstream terms, or upload a video for which you have
the necessary permission.
